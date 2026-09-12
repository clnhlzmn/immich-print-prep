"""Image preparation pipeline.

The default conversion is:

    rotate -90 for landscape images -> pad to the 8x10 aspect ratio with white
    -> 300 DPI -> resize to 8x10 inches -> JPEG q95, no metadata

The pipeline is applied to one image at a time and is fully described by an
`Adjustments` value, so the same code renders the low-resolution previews shown
in the browser and the full-resolution files that go into the zip.
"""

from __future__ import annotations

import io
import re
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PIL import Image, ImageCms, ImageDraw, ImageFont, ImageOps, UnidentifiedImageError

try:  # HEIC/HEIF is what phones shoot; Pillow cannot read it on its own.
    from pillow_heif import register_heif_opener
except ImportError:  # pragma: no cover - always installed in the image
    HEIF_ENABLED = False
else:
    register_heif_opener()
    HEIF_ENABLED = True

# Pillow >= 9.1 exposes resampling filters on Image.Resampling.
LANCZOS = Image.Resampling.LANCZOS

ROTATE_CHOICES = ("auto", "none", "cw", "ccw", "180")
FIT_CHOICES = ("pad", "crop")
FORMAT_CHOICES = ("jpeg", "png", "tiff")

_HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")

# Guard rails: an 8x10 at 300 DPI is 2400x3000; allow generous headroom but
# refuse absurd sizes that would exhaust memory.
MAX_PIXELS_PER_SIDE = 20000

# ---------- caption geometry ----------
CAPTION_FONT_PATH = Path(__file__).parent / "fonts" / "DejaVuSansCondensed.ttf"
CAPTION_EDGE_IN = 0.15          # text keeps this far from the paper edge; labs trim a little
CAPTION_GAP_IN = 0.08           # and this far from the photo
CAPTION_FONT_PT = 10.0
CAPTION_MIN_FONT_PT = 6.5
CAPTION_LINE_SPACING = 1.2
CAPTION_MAX_STRIP_IN = 1.5      # never take more than this from the photo...
CAPTION_MAX_STRIP_FRACTION = 0.2  # ...or this share of a small print
MAX_CAPTION_CHARS = 1000
ELLIPSIS = "\u2026"


class ImagingError(ValueError):
    """Raised for adjustments that cannot be applied."""


class UnsupportedImageError(ImagingError):
    """Raised when the bytes are not an image this build can decode.

    Callers can treat this as "ask Immich for a JPEG rendition instead" rather
    than as a lost photo.
    """


# Magic numbers, purely so the error message can name the format.
_SIGNATURES = (
    (b"\xff\xd8\xff", "JPEG"),
    (b"\x89PNG\r\n", "PNG"),
    (b"GIF8", "GIF"),
    (b"II*\x00", "TIFF or camera raw"),
    (b"MM\x00*", "TIFF or camera raw"),
    (b"BM", "BMP"),
)
_FTYP_BRANDS = {
    b"heic": "HEIC", b"heix": "HEIC", b"heim": "HEIC", b"heis": "HEIC",
    b"hevc": "HEIC", b"mif1": "HEIC", b"msf1": "HEIC", b"heif": "HEIF",
    b"avif": "AVIF", b"crx ": "Canon raw",
}


def sniff_format(data: bytes) -> str:
    """Best guess at what these bytes are, for a legible error message."""
    head = data[:32]
    for signature, name in _SIGNATURES:
        if head.startswith(signature):
            return name
    if head[4:8] == b"ftyp":
        return _FTYP_BRANDS.get(head[8:12].lower(), "ISO media")
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "WebP"
    return "unrecognised data"


def open_image(data: bytes) -> Image.Image:
    """Decode image bytes, or raise `UnsupportedImageError` naming the format."""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        return image
    except UnidentifiedImageError as exc:
        kind = sniff_format(data)
        hint = ""
        if kind in ("HEIC", "HEIF") and not HEIF_ENABLED:
            hint = " (this build has no HEIF support)"
        raise UnsupportedImageError("cannot decode %s%s" % (kind, hint)) from exc
    except OSError as exc:
        raise UnsupportedImageError("could not read the image: %s" % exc) from exc


@dataclass(frozen=True)
class Crop:
    """A crop rectangle in normalised coordinates of the *rotated* image.

    Normalised means 0..1 relative to the image the user sees in the crop
    editor, which is the image after EXIF transposition and after the rotation
    step below. Keeping it in that space means the rectangle the user dragged
    is the rectangle that gets cut, with no orientation bookkeeping in the UI.
    """

    x: float
    y: float
    w: float
    h: float

    @staticmethod
    def parse(value: Any) -> Optional[Crop]:
        if value in (None, "", {}):
            return None
        if isinstance(value, Crop):
            return value
        if not isinstance(value, dict):
            raise ImagingError("crop must be an object with x, y, w, h")
        try:
            crop = Crop(
                x=float(value["x"]), y=float(value["y"]),
                w=float(value["w"]), h=float(value["h"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ImagingError("crop must have numeric x, y, w, h") from exc
        if crop.w <= 0 or crop.h <= 0:
            raise ImagingError("crop width and height must be positive")
        return crop

    def clamped(self) -> Crop:
        x = min(max(self.x, 0.0), 1.0)
        y = min(max(self.y, 0.0), 1.0)
        w = min(self.w, 1.0 - x)
        h = min(self.h, 1.0 - y)
        return Crop(x, y, max(w, 1e-6), max(h, 1e-6))

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)


@dataclass(frozen=True)
class Adjustments:
    """Everything that decides what one prepared file looks like."""

    width_in: float = 8.0
    height_in: float = 10.0
    dpi: int = 300
    rotate: str = "auto"            # auto (rotate landscape CCW) | none | cw | ccw | 180
    fit: str = "pad"                # pad with background | crop to fill
    background: str = "#ffffff"
    quality: int = 95               # JPEG quality
    allow_enlarge: bool = True      # upscale sources smaller than the target
    fmt: str = "jpeg"
    crop: Optional[Crop] = None
    # Caption in the border padding leaves: which of Immich's facts to print,
    # or the user's own text for this photo (None means "use Immich's").
    caption: bool = False
    caption_date: bool = True
    caption_location: bool = True
    caption_gear: bool = True
    caption_description: bool = True
    caption_people: bool = True
    caption_text: Optional[str] = None

    # ---------- (de)serialisation ----------

    @staticmethod
    def from_dict(
        value: Optional[Dict[str, Any]], base: Optional[Adjustments] = None
    ) -> Adjustments:
        """Build adjustments from untrusted JSON, layered over `base`."""
        adj = base or Adjustments()
        if not value:
            return adj
        if not isinstance(value, dict):
            raise ImagingError("adjustments must be an object")

        def num(key: str, current: float, lo: float, hi: float) -> float:
            if key not in value or value[key] is None:
                return current
            try:
                out = float(value[key])
            except (TypeError, ValueError) as exc:
                raise ImagingError("%s must be a number" % key) from exc
            if not lo <= out <= hi:
                raise ImagingError("%s must be between %g and %g" % (key, lo, hi))
            return out

        def choice(key: str, current: str, options: Tuple[str, ...]) -> str:
            if key not in value or value[key] is None:
                return current
            out = str(value[key]).lower()
            if out not in options:
                raise ImagingError("%s must be one of %s" % (key, ", ".join(options)))
            return out

        crop = adj.crop
        if "crop" in value:
            crop = Crop.parse(value["crop"])

        allow_enlarge = adj.allow_enlarge
        if value.get("allow_enlarge") is not None:
            allow_enlarge = bool(value["allow_enlarge"])

        def flag(key: str, current: bool) -> bool:
            return current if value.get(key) is None else bool(value[key])

        caption_text = adj.caption_text
        if "caption_text" in value:
            raw_text = value["caption_text"]
            caption_text = None if raw_text is None else str(raw_text)[:MAX_CAPTION_CHARS]

        out = Adjustments(
            width_in=num("width_in", adj.width_in, 0.5, 60.0),
            height_in=num("height_in", adj.height_in, 0.5, 60.0),
            dpi=int(num("dpi", adj.dpi, 36, 1200)),
            rotate=choice("rotate", adj.rotate, ROTATE_CHOICES),
            fit=choice("fit", adj.fit, FIT_CHOICES),
            background=parse_color(value.get("background") or adj.background, as_hex=True),
            quality=int(num("quality", adj.quality, 1, 100)),
            allow_enlarge=allow_enlarge,
            fmt=choice("fmt", adj.fmt, FORMAT_CHOICES),
            crop=crop,
            caption=flag("caption", adj.caption),
            caption_date=flag("caption_date", adj.caption_date),
            caption_location=flag("caption_location", adj.caption_location),
            caption_gear=flag("caption_gear", adj.caption_gear),
            caption_description=flag("caption_description", adj.caption_description),
            caption_people=flag("caption_people", adj.caption_people),
            caption_text=caption_text,
        )
        w, h = out.target_pixels()
        if w > MAX_PIXELS_PER_SIDE or h > MAX_PIXELS_PER_SIDE:
            raise ImagingError("target size is too large (%dx%d pixels)" % (w, h))
        return out

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["crop"] = self.crop.to_dict() if self.crop else None
        return out

    def without_crop(self) -> Adjustments:
        return replace(self, crop=None)

    # ---------- derived values ----------

    def target_pixels(self) -> Tuple[int, int]:
        return (max(1, round(self.width_in * self.dpi)), max(1, round(self.height_in * self.dpi)))

    def target_ratio(self) -> float:
        return self.width_in / self.height_in

    def extension(self) -> str:
        return {"jpeg": ".jpg", "png": ".png", "tiff": ".tif"}[self.fmt]


def parse_color(value: Any, as_hex: bool = False):
    """Accept `#rrggbb` / `rrggbb` and return an RGB tuple (or normalised hex)."""
    if isinstance(value, (tuple, list)) and len(value) == 3:
        rgb = tuple(int(min(max(int(c), 0), 255)) for c in value)
        return "#%02x%02x%02x" % rgb if as_hex else rgb
    match = _HEX_RE.match(str(value or ""))
    if not match:
        raise ImagingError("colour must be a hex value like #ffffff")
    digits = match.group(1).lower()
    if as_hex:
        return "#" + digits
    return tuple(int(digits[i:i + 2], 16) for i in (0, 2, 4))


def _to_srgb_rgb(image: Image.Image, background: Tuple[int, int, int]) -> Image.Image:
    """Flatten onto the background and land in sRGB, dropping the input profile.

    The profile is not carried into the output, so converting first keeps
    wide-gamut sources (Display P3 phone photos) from shifting when it is
    dropped.
    """
    profile = image.info.get("icc_profile")
    if profile and image.mode in ("RGB", "RGBA", "CMYK", "L"):
        try:
            source = ImageCms.ImageCmsProfile(io.BytesIO(profile))
            target_mode = "RGBA" if image.mode == "RGBA" else "RGB"
            image = ImageCms.profileToProfile(
                image, source, ImageCms.createProfile("sRGB"), outputMode=target_mode
            ) or image
        except Exception:
            # An unreadable profile is not worth failing a print run over.
            pass

    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        flat = Image.new("RGB", image.size, background)
        flat.paste(image, mask=image.split()[-1])
        return flat
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


def _apply_rotation(image: Image.Image, rotate: str) -> Image.Image:
    if rotate == "auto":
        # Landscape only, rotated by -90 degrees; portrait is left as shot.
        return image.transpose(Image.Transpose.ROTATE_90) if image.width > image.height else image
    if rotate == "ccw":
        return image.transpose(Image.Transpose.ROTATE_90)
    if rotate == "cw":
        return image.transpose(Image.Transpose.ROTATE_270)
    if rotate == "180":
        return image.transpose(Image.Transpose.ROTATE_180)
    return image


def _apply_crop(image: Image.Image, crop: Crop) -> Image.Image:
    c = crop.clamped()
    left = int(round(c.x * image.width))
    top = int(round(c.y * image.height))
    right = max(left + 1, int(round((c.x + c.w) * image.width)))
    bottom = max(top + 1, int(round((c.y + c.h) * image.height)))
    right = min(right, image.width)
    bottom = min(bottom, image.height)
    return image.crop((left, top, right, bottom))


def _fit_pad(
    image: Image.Image, size: Tuple[int, int], background, allow_enlarge: bool
) -> Image.Image:
    target_w, target_h = size
    scale = min(target_w / image.width, target_h / image.height)
    if not allow_enlarge:
        scale = min(scale, 1.0)
    new_w = max(1, int(round(image.width * scale)))
    new_h = max(1, int(round(image.height * scale)))
    if (new_w, new_h) != image.size:
        image = image.resize((new_w, new_h), LANCZOS)
    canvas = Image.new("RGB", (target_w, target_h), background)
    canvas.paste(image, ((target_w - new_w) // 2, (target_h - new_h) // 2))
    return canvas


def _fit_crop(
    image: Image.Image, size: Tuple[int, int], background, allow_enlarge: bool
) -> Image.Image:
    target_w, target_h = size
    scale = max(target_w / image.width, target_h / image.height)
    if not allow_enlarge and scale > 1.0:
        # Too small to fill the frame; centre it on the background instead of
        # blowing it up.
        return _fit_pad(image, size, background, allow_enlarge=False)
    new_w = max(1, int(round(image.width * scale)))
    new_h = max(1, int(round(image.height * scale)))
    if (new_w, new_h) != image.size:
        image = image.resize((new_w, new_h), LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return image.crop((left, top, left + target_w, top + target_h))


# ---------- captions ----------

def _quarter_turns(rotate: str, upright_landscape: bool) -> int:
    """Counter-clockwise quarter turns `_apply_rotation` gives this photo."""
    if rotate == "auto":
        return 1 if upright_landscape else 0
    return {"ccw": 1, "180": 2, "cw": 3}.get(rotate, 0)


@lru_cache(maxsize=32)
def _caption_font(px: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(CAPTION_FONT_PATH), max(1, px))


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: float) -> List[str]:
    lines: List[str] = []
    for paragraph in text.splitlines():
        words = paragraph.split()
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if font.getlength(candidate) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            # A single word wider than the line (a URL, say) breaks by character.
            while len(word) > 1 and font.getlength(word) > width:
                cut = len(word) - 1
                while cut > 1 and font.getlength(word[:cut]) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        if current:
            lines.append(current)
    return lines


def _truncate(
    lines: List[str], max_lines: int, font: ImageFont.FreeTypeFont, width: float
) -> Tuple[List[str], bool]:
    if len(lines) <= max_lines:
        return lines, False
    kept = lines[: max(1, max_lines)]
    last = kept[-1]
    while last and font.getlength(last + ELLIPSIS) > width:
        last = last[:-1].rstrip()
    kept[-1] = last + ELLIPSIS
    return kept, True


def _elide(text: str, font, width: float) -> str:
    """`text` cut to `width` with an ellipsis. Empty if not even that fits."""
    if font.getlength(text) <= width:
        return text
    while text and font.getlength(text + ELLIPSIS) > width:
        text = text[:-1].rstrip()
    return text + ELLIPSIS if text else ""


def _fit_gear(
    atoms: Sequence[str], left: Sequence[str], font, line_width: int, max_rows: int
) -> Tuple[str, ...]:
    """Pack the gear into right-aligned rows beside the prose, as two columns.

    The two sides are columns, not independent rows: the gear gets whatever the
    *widest* prose line leaves, and every gear row is held to that same width.
    Measuring each row against only its own prose line would let a wide gear row
    reach back under a short one and read as a continuation of the prose above
    it - "hi some long lens name" sitting under "2026-09-12    SONY".

    Within that column the atoms are the smallest pieces worth keeping whole -
    body, lens, then each exposure setting - packed onto as many rows as they
    need, so a row that cannot take the lens passes it to the next one rather
    than the caption losing it. Rows past the end of the prose do add height;
    that only happens when the gear needs more rows than the prose has lines.
    """
    pending = [atom for atom in atoms if atom]
    if not pending:
        return ()
    widest = max((font.getlength(line) for line in left), default=0.0)
    room = line_width - (widest + font.getlength("  ") if widest else 0)
    if room <= 0:
        return ()

    rows: List[str] = []
    while pending and len(rows) < max_rows:
        taken: List[str] = []
        while pending and font.getlength(" · ".join(taken + pending[:1])) <= room:
            taken.append(pending.pop(0))
        if not taken:
            # The column is the same width on every row, so a piece that does
            # not fit here will not fit below either.
            short = _elide(pending.pop(0), font, room)
            if not short:
                break
            taken.append(short)
        rows.append(" · ".join(taken))
    return tuple(rows)


@dataclass(frozen=True)
class CaptionLayout:
    """Where the photo and its caption go on the canvas."""

    canvas: Tuple[int, int]
    photo_box: Tuple[int, int, int, int]   # left, top, width, height
    edge: str                              # right | left | bottom | top
    strip: int                             # caption strip: photo edge to paper edge
    lines: Tuple[str, ...]
    # Gear, right-aligned on the same rows as `lines` so it costs no height.
    right_lines: Tuple[str, ...]
    font_px: int
    line_height: int
    edge_px: int
    gap_px: int
    step: str                              # centred | slid | shrunk
    truncated: bool = False

    def scaled(self, factor: float) -> CaptionLayout:
        """The same layout for a smaller proof - identical line breaks."""
        def size(value: float) -> int:
            return max(1, int(round(value * factor)))

        left, top, width, height = self.photo_box
        return replace(
            self,
            canvas=(size(self.canvas[0]), size(self.canvas[1])),
            photo_box=(
                int(round(left * factor)), int(round(top * factor)), size(width), size(height)
            ),
            strip=size(self.strip),
            font_px=size(self.font_px),
            line_height=size(self.line_height),
            edge_px=int(round(self.edge_px * factor)),
            gap_px=int(round(self.gap_px * factor)),
        )


def _fit_within(
    size: Tuple[int, int], box: Tuple[int, int], allow_enlarge: bool
) -> Tuple[int, int]:
    factor = min(box[0] / size[0], box[1] / size[1])
    if not allow_enlarge:
        factor = min(factor, 1.0)
    return max(1, int(round(size[0] * factor))), max(1, int(round(size[1] * factor)))


def _layout_along(
    along_sides: bool,
    photo_size: Tuple[int, int],
    canvas: Tuple[int, int],
    text: str,
    dpi: float,
    turns: int,
    allow_enlarge: bool,
    gear: Sequence[str] = (),
) -> CaptionLayout:
    """Fit the caption on one pair of edges: down the sides, or along the ends."""
    width, height = canvas

    def px(inches: float) -> int:
        return int(round(inches * dpi))

    edge_px, gap_px = px(CAPTION_EDGE_IN), px(CAPTION_GAP_IN)
    fit_w, fit_h = _fit_within(photo_size, canvas, allow_enlarge)
    turns %= 4
    if along_sides:
        edge = "left" if turns in (2, 3) else "right"
        run, space, across = height, width - fit_w, width
    else:
        edge = "top" if turns == 2 else "bottom"
        run, space, across = width, height - fit_h, height
    line_width = max(1, run - 2 * edge_px)
    max_strip = max(
        edge_px + gap_px + 1,
        min(px(CAPTION_MAX_STRIP_IN), int(across * CAPTION_MAX_STRIP_FRACTION)),
    )

    # Worst case one atom per row, so this many rows never drops one - and the
    # packer stops as soon as they are placed.
    gear_rows = len([atom for atom in gear if atom])

    def measure(point_size: float):
        font_px = max(1, int(round(point_size / 72.0 * dpi)))
        line_height = max(1, int(round(font_px * CAPTION_LINE_SPACING)))
        font = _caption_font(font_px)
        lines = _wrap(text, font, line_width)
        right = _fit_gear(gear, lines, font, line_width, max(len(lines), gear_rows))
        rows = max(len(lines), len(right))
        return font_px, line_height, lines, right, edge_px + gap_px + rows * line_height

    point_size = CAPTION_FONT_PT
    font_px, line_height, lines, right_lines, need = measure(point_size)
    while need > space and point_size > CAPTION_MIN_FONT_PT:
        point_size = max(CAPTION_MIN_FONT_PT, point_size - 0.5)
        font_px, line_height, lines, right_lines, need = measure(point_size)

    truncated = False
    limit = max(max_strip, space)
    if need > limit:
        max_rows = max(1, (limit - edge_px - gap_px) // line_height)
        font = _caption_font(font_px)
        lines, truncated = _truncate(lines, max_rows, font, line_width)
        # The prose just moved, so the gear has to be re-fitted against it,
        # within the rows that are left.
        right_lines = _fit_gear(gear, lines, font, line_width, max_rows)
        need = edge_px + gap_px + max(len(lines), len(right_lines)) * line_height

    if need <= space:
        photo_w, photo_h = fit_w, fit_h
        left, top = (width - fit_w) // 2, (height - fit_h) // 2
        if need * 2 <= space:
            step = "centred"
        else:
            step = "slid"
            if edge == "right":
                left = width - need - fit_w
            elif edge == "left":
                left = need
            elif edge == "bottom":
                top = height - need - fit_h
            else:
                top = need
    else:
        step = "shrunk"
        if along_sides:
            photo_w, photo_h = _fit_within(photo_size, (width - need, height), allow_enlarge)
            left = (width - need - photo_w) // 2 + (need if edge == "left" else 0)
            top = (height - photo_h) // 2
        else:
            photo_w, photo_h = _fit_within(photo_size, (width, height - need), allow_enlarge)
            left = (width - photo_w) // 2
            top = (height - need - photo_h) // 2 + (need if edge == "top" else 0)

    strip = {
        "right": width - (left + photo_w),
        "left": left,
        "bottom": height - (top + photo_h),
        "top": top,
    }[edge]
    return CaptionLayout(
        canvas=(width, height),
        photo_box=(left, top, photo_w, photo_h),
        edge=edge,
        strip=strip,
        lines=tuple(lines),
        right_lines=tuple(right_lines),
        font_px=font_px,
        line_height=line_height,
        edge_px=edge_px,
        gap_px=gap_px,
        step=step,
        truncated=truncated,
    )


def layout_caption(
    photo_size: Tuple[int, int],
    canvas: Tuple[int, int],
    text: str,
    dpi: float,
    turns: int = 0,
    allow_enlarge: bool = True,
    gear: Sequence[str] = (),
) -> CaptionLayout:
    """Place a caption so the photo stays as large as possible.

    First choice is the border padding already leaves: down the right edge
    (text turned counter-clockwise, so a landscape photo turned onto portrait
    paper reads it underneath once the print is turned back) or along the
    bottom, mirrored for photos turned clockwise or upside down. Within that
    edge, cheapest first: keep the photo centred, slide it away from the
    caption, shrink the font.

    Only if the photo must shrink anyway does the edge stop being fixed: both
    are tried and the one that leaves more photo wins. A 2:3 photo fills a 4x6
    print exactly, and a strip along the long side costs it less than one down
    the short side.
    """
    fit_w, fit_h = _fit_within(photo_size, canvas, allow_enlarge)
    natural_sides = canvas[0] - fit_w >= canvas[1] - fit_h
    preferred = _layout_along(
        natural_sides, photo_size, canvas, text, dpi, turns, allow_enlarge, gear
    )
    if preferred.step != "shrunk":
        return preferred
    other = _layout_along(
        not natural_sides, photo_size, canvas, text, dpi, turns, allow_enlarge, gear
    )
    if other.step != "shrunk":
        return other

    def photo_area(layout: CaptionLayout) -> int:
        return layout.photo_box[2] * layout.photo_box[3]

    return other if photo_area(other) > photo_area(preferred) else preferred


def _text_colour(background: Tuple[int, int, int]) -> Tuple[int, int, int]:
    r, g, b = background
    return (32, 32, 32) if 0.299 * r + 0.587 * g + 0.114 * b >= 140 else (238, 238, 238)


def _draw_caption(photo: Image.Image, layout: CaptionLayout, background) -> Image.Image:
    width, height = layout.canvas
    left, top, photo_w, photo_h = layout.photo_box
    canvas = Image.new("RGB", (width, height), background)
    if photo.size != (photo_w, photo_h):
        photo = photo.resize((photo_w, photo_h), LANCZOS)
    canvas.paste(photo, (left, top))

    # Lay the lines out as ordinary horizontal text, first line nearest the
    # photo, then turn the band to fit its edge.
    run = height if layout.edge in ("right", "left") else width
    strip = max(1, layout.strip)
    band = Image.new("RGB", (run, strip), background)
    draw = ImageDraw.Draw(band)
    font = _caption_font(layout.font_px)
    colour = _text_colour(background)
    for index, line in enumerate(layout.lines):
        draw.text(
            (layout.edge_px, layout.gap_px + index * layout.line_height),
            line, font=font, fill=colour,
        )
    # Gear hugs the far end of the band, on the rows the prose already occupies.
    for index, line in enumerate(layout.right_lines):
        draw.text(
            (run - layout.edge_px - font.getlength(line),
             layout.gap_px + index * layout.line_height),
            line, font=font, fill=colour,
        )
    if layout.edge == "right":
        canvas.paste(band.transpose(Image.Transpose.ROTATE_90), (width - strip, 0))
    elif layout.edge == "left":
        canvas.paste(band.transpose(Image.Transpose.ROTATE_270), (0, 0))
    elif layout.edge == "bottom":
        canvas.paste(band, (0, height - strip))
    else:
        canvas.paste(band.transpose(Image.Transpose.ROTATE_180), (0, 0))
    return canvas


def render(
    source: Image.Image, adj: Adjustments, scale: float = 1.0, caption: Optional[str] = None,
    gear: Sequence[str] = (),
) -> Image.Image:
    """Run the pipeline and return the finished RGB image.

    `scale` renders a proportionally smaller version (used for previews); the
    geometry is identical, only the pixel count differs. A non-blank `caption`
    is printed in the border, which means the photo is always padded, never
    cropped to fill.
    """
    background = parse_color(adj.background)
    image = ImageOps.exif_transpose(source) or source
    image = _to_srgb_rgb(image, background)
    upright_landscape = image.width > image.height
    image = _apply_rotation(image, adj.rotate)
    if adj.crop is not None:
        image = _apply_crop(image, adj.crop)

    text = (caption or "").strip()
    gear = tuple(group for group in gear if group)
    if text or gear:
        # Laid out at full print resolution so a proof wraps exactly like the file.
        layout = layout_caption(
            image.size, adj.target_pixels(), text, adj.dpi,
            _quarter_turns(adj.rotate, upright_landscape), adj.allow_enlarge, gear,
        )
        if scale != 1.0:
            layout = layout.scaled(scale)
        return _draw_caption(image, layout, background)

    target_w, target_h = adj.target_pixels()
    if scale != 1.0:
        target_w = max(1, int(round(target_w * scale)))
        target_h = max(1, int(round(target_h * scale)))

    fit = _fit_crop if adj.fit == "crop" else _fit_pad
    return fit(image, (target_w, target_h), background, adj.allow_enlarge)


def encode(image: Image.Image, adj: Adjustments) -> bytes:
    """Serialise a rendered image, stamping the print DPI into the file."""
    buf = io.BytesIO()
    dpi = (adj.dpi, adj.dpi)
    if adj.fmt == "jpeg":
        image.save(
            buf, format="JPEG", quality=adj.quality, optimize=True,
            subsampling=0,  # 4:4:4 - no chroma loss on a file destined for print
            dpi=dpi,
        )
    elif adj.fmt == "png":
        image.save(buf, format="PNG", dpi=dpi, optimize=True)
    else:
        image.save(buf, format="TIFF", dpi=dpi, compression="tiff_lzw")
    return buf.getvalue()


def prepare(
    data: bytes, adj: Adjustments, caption: Optional[str] = None, gear: Sequence[str] = ()
) -> bytes:
    """Render and encode raw source bytes in one step."""
    with open_image(data) as source:
        rendered = render(source, adj, caption=caption, gear=gear)
    return encode(rendered, adj)


def preview(
    data: bytes, adj: Adjustments, max_edge: int = 900, caption: Optional[str] = None,
    gear: Sequence[str] = (),
) -> bytes:
    """Render a small, fast, visually identical proof of `prepare`."""
    target_w, target_h = adj.target_pixels()
    scale = min(1.0, max_edge / max(target_w, target_h))
    with open_image(data) as source:
        rendered = render(source, adj, scale=scale, caption=caption, gear=gear)
    buf = io.BytesIO()
    rendered.save(buf, format="JPEG", quality=82, optimize=True)
    return buf.getvalue()


def source_geometry(data: bytes, adj: Adjustments) -> Dict[str, Any]:
    """Size and aspect of the image *as the crop editor sees it* (post-rotate)."""
    with open_image(data) as source:
        image = ImageOps.exif_transpose(source) or source
        image = _apply_rotation(image, adj.rotate)
        return {"width": image.width, "height": image.height}


def oriented_source(data: bytes, adj: Adjustments, max_edge: int = 1400) -> bytes:
    """The image the crop editor draws on: EXIF-corrected, rotated, downscaled.

    The crop rectangle is normalised against exactly this image, so what the
    user drags is what gets cut.
    """
    with open_image(data) as source:
        image = ImageOps.exif_transpose(source) or source
        image = _to_srgb_rgb(image, parse_color(adj.background))
        image = _apply_rotation(image, adj.rotate)
    if max(image.size) > max_edge:
        scale = max_edge / max(image.size)
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))), LANCZOS
        )
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()
