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
from typing import Any, Dict, Optional, Tuple

from PIL import Image, ImageCms, ImageOps, UnidentifiedImageError

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


def render(source: Image.Image, adj: Adjustments, scale: float = 1.0) -> Image.Image:
    """Run the pipeline and return the finished RGB image.

    `scale` renders a proportionally smaller version (used for previews); the
    geometry is identical, only the pixel count differs.
    """
    background = parse_color(adj.background)
    image = ImageOps.exif_transpose(source) or source
    image = _to_srgb_rgb(image, background)
    image = _apply_rotation(image, adj.rotate)
    if adj.crop is not None:
        image = _apply_crop(image, adj.crop)

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


def prepare(data: bytes, adj: Adjustments) -> bytes:
    """Render and encode raw source bytes in one step."""
    with open_image(data) as source:
        rendered = render(source, adj)
    return encode(rendered, adj)


def preview(data: bytes, adj: Adjustments, max_edge: int = 900) -> bytes:
    """Render a small, fast, visually identical proof of `prepare`."""
    target_w, target_h = adj.target_pixels()
    scale = min(1.0, max_edge / max(target_w, target_h))
    with open_image(data) as source:
        rendered = render(source, adj, scale=scale)
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
