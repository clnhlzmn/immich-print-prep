"""Caption text for prints: when and where a photo was taken, its description,
who is in it, and what it was shot with.

Everything here is pure except `resolve_source`, which asks Immich for the
facts. Where the caption goes on the print, and drawing it, live in imaging.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .immich import ImmichClient, ImmichError

# Heads the people line. Deliberately not "From left to right": unnamed faces
# are left out, so the names are in order but are not every face in the photo.
PEOPLE_PREFIX = "In this photo: "

# Immich stores either an IANA zone ("America/Chicago") or a bare offset
# ("UTC-5", "UTC+5:30", "UTC+0").
_OFFSET_ZONE_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)
_LETTER_ABBREVIATION_RE = re.compile(r"^[A-Z]{2,5}$")


@dataclass(frozen=True)
class Face:
    """One detected face, positioned as fractions of the upright photo."""

    name: str      # "" when nobody has named this person (or they are hidden)
    cx: float
    cy: float
    w: float
    h: float


@dataclass
class CaptionSource:
    """What Immich knows about a photo, before the user's toggles are applied."""

    capture: Optional[str] = None
    location: Optional[str] = None
    description: Optional[str] = None
    # The two gear groups, printed right-aligned rather than as prose lines.
    camera: Optional[str] = None
    exposure: Optional[str] = None
    faces: List[Face] = field(default_factory=list)
    # From the face detector's image size; decides what "auto" rotation did.
    upright_landscape: Optional[bool] = None
    # Named people with no positions, for keys that cannot read faces.
    names: List[str] = field(default_factory=list)
    faces_available: bool = True
    notes: List[str] = field(default_factory=list)
    # Immich failed in a way that may not happen next time; not worth remembering.
    transient: bool = False


# ---------- capture time ----------

def _parse_timestamp(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _zone(name: Optional[str]):
    if not name:
        return None
    name = str(name).strip()
    if name.upper() in ("UTC", "GMT", "Z"):
        return timezone.utc
    offset = _OFFSET_ZONE_RE.match(name)
    if offset:
        sign, hours, minutes = offset.group(1), int(offset.group(2)), int(offset.group(3) or 0)
        delta = timedelta(hours=hours, minutes=minutes)
        return timezone(-delta if sign == "-" else delta)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _zone_label(moment: datetime) -> str:
    """`CDT` where the zone has a letter abbreviation, otherwise `UTC+05:30`."""
    abbreviation = moment.tzname() or ""
    if _LETTER_ABBREVIATION_RE.match(abbreviation):
        return abbreviation
    minutes = int((moment.utcoffset() or timedelta(0)).total_seconds() // 60)
    if minutes == 0:
        return "UTC"
    sign = "+" if minutes > 0 else "-"
    minutes = abs(minutes)
    return "UTC%s%02d:%02d" % (sign, minutes // 60, minutes % 60)


def format_capture_time(
    date_time_original: Optional[str],
    time_zone: Optional[str],
    local_date_time: Optional[str],
) -> Optional[str]:
    """`YYYY-MM-DD HH:MM:SS TZ` in the zone the photo was taken in.

    With a zone, `dateTimeOriginal` (a true instant) is shown in that zone.
    Without one, Immich's `localDateTime` is the camera's own clock reading and
    the zone is genuinely unknown, so it is printed with no zone at all rather
    than a guessed one.
    """
    zone = _zone(time_zone)
    instant = _parse_timestamp(date_time_original)
    if zone is not None and instant is not None:
        local = instant.astimezone(zone)
        return "%s %s" % (local.strftime("%Y-%m-%d %H:%M:%S"), _zone_label(local))
    wall_clock = _parse_timestamp(local_date_time) or instant
    if wall_clock is None:
        return None
    return wall_clock.strftime("%Y-%m-%d %H:%M:%S")


# ---------- place ----------

def format_location(
    city: Optional[str], state: Optional[str], country: Optional[str]
) -> Optional[str]:
    """`City, State, Country` from whichever parts Immich reverse-geocoded.

    Immich fills these in from the photo's GPS fix, so a photo without one has
    no location line at all. A part repeated by the geocoder (Singapore the
    city in Singapore the country) is printed once.
    """
    parts: List[str] = []
    for value in (city, state, country):
        text = (value or "").strip()
        if text and text.casefold() not in [part.casefold() for part in parts]:
            parts.append(text)
    return ", ".join(parts) or None


# ---------- camera and exposure ----------

def _number(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def format_camera(
    make: Optional[str], model: Optional[str], lens: Optional[str]
) -> Optional[str]:
    """`Canon EOS R6 · RF24-70mm F2.8 L IS USM`, from whichever parts exist.

    Only the brand word of the make is used, and only when the model does not
    already carry it. EXIF makes are inconsistent about this: Canon writes
    "Canon" / "Canon EOS R6" and Nikon "NIKON CORPORATION" / "NIKON Z 6", both
    of which would read twice, while Sony writes "SONY" / "ILCE-7M4" and Apple
    "Apple" / "iPhone 15 Pro", which need the brand to make sense.
    """
    make, model, lens = (str(part or "").strip() for part in (make, model, lens))
    brand = make.split()[0] if make else ""
    body = model
    if brand and not model.casefold().startswith(brand.casefold()):
        body = ("%s %s" % (brand, model)).strip()
    return " · ".join(part for part in (body, lens) if part) or None


def format_exposure(
    focal_length: Any, f_number: Any, exposure_time: Any
) -> Optional[str]:
    """`50mm · f/2.8 · 1/250s`, from whichever settings the camera recorded."""
    parts: List[str] = []
    focal = _number(focal_length)
    if focal:
        parts.append("%gmm" % round(focal, 1))
    aperture = _number(f_number)
    if aperture:
        parts.append("f/%g" % round(aperture, 1))
    shutter = _format_shutter(exposure_time)
    if shutter:
        parts.append(shutter)
    return " · ".join(parts) or None


def _format_shutter(value: Any) -> Optional[str]:
    """`1/250s` or `1.3s`. Immich reports either a fraction or a bare number."""
    text = str(value or "").strip()
    if not text:
        return None
    if "/" in text:
        return "%ss" % text.replace(" ", "")
    seconds = _number(text)
    if seconds is None or seconds <= 0:
        return None
    if seconds >= 1:
        return "%gs" % round(seconds, 1)
    return "1/%ds" % round(1 / seconds)


# ---------- people ----------

def parse_faces(payload: Any) -> Tuple[List[Face], Optional[bool]]:
    """Faces from Immich's GET /faces, plus whether the upright photo is landscape."""
    faces: List[Face] = []
    landscape: Optional[bool] = None
    for entry in payload or []:
        if not isinstance(entry, dict):
            continue
        width, height = entry.get("imageWidth") or 0, entry.get("imageHeight") or 0
        if width <= 0 or height <= 0:
            continue
        x1, x2, y1, y2 = (
            float(entry.get(key) or 0)
            for key in ("boundingBoxX1", "boundingBoxX2", "boundingBoxY1", "boundingBoxY2")
        )
        person = entry.get("person") or {}
        name = "" if person.get("isHidden") else str(person.get("name") or "").strip()
        faces.append(
            Face(
                name=name,
                cx=(x1 + x2) / 2 / width,
                cy=(y1 + y2) / 2 / height,
                w=abs(x2 - x1) / width,
                h=abs(y2 - y1) / height,
            )
        )
        landscape = width > height
    return faces, landscape


def quarter_turns(rotate: str, upright_landscape: Optional[bool]) -> int:
    """Counter-clockwise quarter turns the print pipeline applies to a photo."""
    if rotate == "auto":
        return 1 if upright_landscape else 0
    return {"ccw": 1, "180": 2, "cw": 3}.get(rotate, 0)


def to_rotated(u: float, v: float, turns: int) -> Tuple[float, float]:
    """Map a point in the upright photo into the rotated photo the crop box is drawn on."""
    turns %= 4
    if turns == 1:
        return v, 1 - u
    if turns == 2:
        return 1 - u, 1 - v
    if turns == 3:
        return 1 - v, u
    return u, v


def people_line(faces: Sequence[Face], crop: Any = None, turns: int = 0) -> Optional[str]:
    """`In this photo: Alice, Bob`, named left to right as the photo is viewed upright.

    Only named people are listed; anyone unnamed (or hidden) is left out, as is
    anyone the crop removes.
    """
    named = []
    for face in faces:
        if not face.name:
            continue
        if crop is not None:
            x, y = to_rotated(face.cx, face.cy, turns)
            if not (crop.x <= x <= crop.x + crop.w and crop.y <= y <= crop.y + crop.h):
                continue
        named.append(face)
    named.sort(key=lambda face: face.cx)

    names: List[str] = []
    for face in named:
        if face.name not in names:
            names.append(face.name)
    return PEOPLE_PREFIX + ", ".join(names) if names else None


def unordered_people_line(names: Sequence[str]) -> Optional[str]:
    unique: List[str] = []
    for name in names:
        name = (name or "").strip()
        if name and name not in unique:
            unique.append(name)
    return PEOPLE_PREFIX + ", ".join(unique) if unique else None


# ---------- putting it together ----------

def caption_parts(source: CaptionSource, adj: Any) -> Dict[str, Optional[str]]:
    """Immich's caption lines for this photo, with the user's toggles applied."""
    people = None
    if adj.caption_people:
        if source.faces_available:
            people = people_line(
                source.faces, adj.crop, quarter_turns(adj.rotate, source.upright_landscape)
            )
        else:
            people = unordered_people_line(source.names)
    description = None
    if adj.caption_description:
        description = (source.description or "").strip() or None
    return {
        "capture": source.capture if adj.caption_date else None,
        "location": source.location if adj.caption_location else None,
        "description": description,
        "people": people,
        "camera": source.camera if adj.caption_gear else None,
        "exposure": source.exposure if adj.caption_gear else None,
    }


def compose(source: CaptionSource, adj: Any) -> str:
    """The prose of the caption: the user's own text if they wrote one, else the
    parts of Immich's data they switched on, one per line.

    The gear groups are not in here. They are printed right-aligned on the same
    rows (see `compose_gear`), so they cost the photo no height and a rewritten
    caption does not take them away.
    """
    if adj.caption_text is not None:
        return adj.caption_text.strip()
    parts = caption_parts(source, adj)
    lines = (parts["capture"], parts["location"], parts["description"], parts["people"])
    return "\n".join(line for line in lines if line)


def compose_gear(source: CaptionSource, adj: Any) -> Tuple[str, ...]:
    """What the photo was shot with, as groups the layout may split or drop.

    Unlike the prose this survives a hand-written caption: the user rewrites
    what the photo is, not what took it.
    """
    if not adj.caption_gear:
        return ()
    return tuple(group for group in (source.camera, source.exposure) if group)


async def resolve_source(client: ImmichClient, asset_id: str) -> CaptionSource:
    """Gather caption facts from Immich, degrading rather than failing.

    A photo is still worth printing without its caption, so problems become
    notes the job can report instead of exceptions.
    """
    source = CaptionSource()
    try:
        details = await client.asset_details(asset_id)
    except ImmichError as exc:
        source.notes.append("caption unavailable: %s" % exc.message)
        source.transient = True
        return source

    source.capture = format_capture_time(
        details.get("dateTimeOriginal"), details.get("timeZone"), details.get("localDateTime")
    )
    source.location = format_location(
        details.get("city"), details.get("state"), details.get("country")
    )
    source.camera = format_camera(
        details.get("make"), details.get("model"), details.get("lensModel")
    )
    source.exposure = format_exposure(
        details.get("focalLength"), details.get("fNumber"), details.get("exposureTime")
    )
    source.description = (details.get("description") or "").strip() or None
    source.names = [
        person["name"] for person in details.get("people") or []
        if person.get("name") and not person.get("isHidden")
    ]

    try:
        source.faces, source.upright_landscape = parse_faces(await client.faces(asset_id))
    except ImmichError as exc:
        source.faces_available = False
        if exc.status == 403:
            source.notes.append(
                "people listed without left-to-right order: the API key lacks face.read"
            )
        else:
            source.notes.append("people listed without order: %s" % exc.message)
            source.transient = True
    return source
