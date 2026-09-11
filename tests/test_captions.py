"""Caption text: capture time in the photo's own zone, and people left to right."""

from __future__ import annotations

from types import SimpleNamespace

from immich_print_prep.captions import (
    CaptionSource,
    Face,
    compose,
    format_capture_time,
    parse_faces,
    people_line,
    quarter_turns,
    to_rotated,
    unordered_people_line,
)

SUMMER = "2026-07-04T19:30:05.000Z"
WINTER = "2026-01-15T19:30:05.000Z"


# ---------- capture time ----------

def test_a_named_zone_shows_its_abbreviation_and_follows_dst():
    assert format_capture_time(SUMMER, "America/Chicago", None) == "2026-07-04 14:30:05 CDT"
    assert format_capture_time(WINTER, "America/Chicago", None) == "2026-01-15 13:30:05 CST"


def test_an_offset_zone_shows_the_offset():
    assert format_capture_time(SUMMER, "UTC+5:30", None) == "2026-07-05 01:00:05 UTC+05:30"
    assert format_capture_time(SUMMER, "UTC-5", None) == "2026-07-04 14:30:05 UTC-05:00"
    assert format_capture_time(SUMMER, "UTC+0", None) == "2026-07-04 19:30:05 UTC"


def test_a_zone_with_only_a_numeric_abbreviation_shows_the_offset():
    # tzdata calls Dubai "+04"; print something readable instead.
    assert format_capture_time(SUMMER, "Asia/Dubai", None) == "2026-07-04 23:30:05 UTC+04:00"


def test_without_a_zone_the_cameras_clock_is_printed_with_no_zone():
    assert format_capture_time(SUMMER, None, "2026-07-04T14:30:05.000Z") == "2026-07-04 14:30:05"


def test_an_unrecognised_zone_falls_back_to_the_cameras_clock():
    assert format_capture_time(SUMMER, "Mars/Olympus_Mons", "2026-07-04T14:30:05.000Z") == (
        "2026-07-04 14:30:05"
    )


def test_no_dates_at_all_means_no_date_line():
    assert format_capture_time(None, "America/Chicago", None) is None


# ---------- people ----------

def face(name, cx, h=0.2, cy=0.5):
    return Face(name=name, cx=cx, cy=cy, w=h * 0.8, h=h)


GROUP = [
    face("Bob", 0.70),
    face("Alice", 0.20),
    face("", 0.45, h=0.18),       # someone unnamed standing between them
    face("", 0.90, h=0.03),       # and another in the background
]


def test_named_people_are_listed_left_to_right_and_the_unnamed_left_out():
    assert people_line(GROUP) == "From left to right: Alice, Bob"


def test_one_person_is_just_their_name():
    assert people_line([face("Alice", 0.5)]) == "Alice"


def test_nobody_named_means_no_people_line():
    assert people_line([face("", 0.3), face("", 0.6)]) is None


def test_someone_seen_twice_is_named_once():
    assert people_line([face("Alice", 0.2), face("Alice", 0.8)]) == "Alice"


def test_faces_the_crop_cuts_out_are_dropped():
    left_half = SimpleNamespace(x=0.0, y=0.0, w=0.5, h=1.0)
    assert people_line(GROUP, crop=left_half, turns=0) == "Alice"


def test_crop_is_checked_in_the_rotated_photo_the_crop_box_was_drawn_on():
    # A landscape photo turned counter-clockwise: its left side becomes the
    # bottom of the rotated image, so a crop of the bottom half keeps Alice.
    bottom_half = SimpleNamespace(x=0.0, y=0.5, w=1.0, h=0.5)
    assert people_line([face("Alice", 0.2), face("Bob", 0.8)], crop=bottom_half, turns=1) == "Alice"


def test_rotation_helpers_agree_with_the_pipeline():
    assert quarter_turns("auto", True) == 1 and quarter_turns("auto", False) == 0
    assert quarter_turns("cw", None) == 3 and quarter_turns("none", True) == 0
    assert to_rotated(0.0, 0.0, 1) == (0.0, 1.0)    # top-left ends up bottom-left
    assert to_rotated(0.0, 0.0, 3) == (1.0, 0.0)    # ...or top-right, turning the other way


def test_names_without_positions_are_not_presented_as_an_order():
    assert unordered_people_line(["Bob", "Alice", "Bob", ""]) == "With: Bob, Alice"
    assert unordered_people_line([]) is None


def test_faces_are_read_from_immichs_response():
    payload = [
        {"imageWidth": 2000, "imageHeight": 1000, "boundingBoxX1": 100, "boundingBoxX2": 300,
         "boundingBoxY1": 200, "boundingBoxY2": 400, "person": {"name": "Alice", "isHidden": False}},
        {"imageWidth": 2000, "imageHeight": 1000, "boundingBoxX1": 1500, "boundingBoxX2": 1700,
         "boundingBoxY1": 200, "boundingBoxY2": 400, "person": {"name": "Hidden", "isHidden": True}},
        {"imageWidth": 2000, "imageHeight": 1000, "boundingBoxX1": 900, "boundingBoxX2": 1100,
         "boundingBoxY1": 200, "boundingBoxY2": 400, "person": None},
    ]
    faces, landscape = parse_faces(payload)
    assert landscape is True
    assert [(f.name, round(f.cx, 2)) for f in faces] == [("Alice", 0.1), ("", 0.8), ("", 0.5)]


# ---------- compose ----------

def adjustments(**overrides):
    base = dict(caption_text=None, caption_date=True, caption_description=True,
                caption_people=True, crop=None, rotate="auto")
    base.update(overrides)
    return SimpleNamespace(**base)


SOURCE = CaptionSource(
    capture="2026-07-04 14:30:05 CDT",
    description="Fourth of July at the lake",
    faces=[face("Alice", 0.2), face("Bob", 0.7)],
    upright_landscape=True,
)


def test_compose_puts_each_part_on_its_own_line():
    assert compose(SOURCE, adjustments()) == (
        "2026-07-04 14:30:05 CDT\nFourth of July at the lake\nFrom left to right: Alice, Bob"
    )


def test_compose_respects_the_toggles():
    assert compose(SOURCE, adjustments(caption_description=False, caption_people=False)) == (
        "2026-07-04 14:30:05 CDT"
    )


def test_the_users_own_text_wins_even_when_blank():
    assert compose(SOURCE, adjustments(caption_text="  Grandma's 90th  ")) == "Grandma's 90th"
    assert compose(SOURCE, adjustments(caption_text="")) == ""


def test_without_face_access_people_are_listed_unordered():
    source = CaptionSource(names=["Bob", "Alice"], faces_available=False)
    assert compose(source, adjustments(caption_date=False)) == "With: Bob, Alice"
