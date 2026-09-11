"""The conversion pipeline: the part that decides what gets printed."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from immich_print_prep.imaging import (
    Adjustments,
    ImagingError,
    UnsupportedImageError,
    encode,
    prepare,
    render,
    sniff_format,
)

from fake_immich import make_image


def open_result(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


def test_landscape_is_rotated_and_padded_to_8x10_at_300dpi():
    result = prepare(make_image(4000, 3000), Adjustments())
    image = open_result(result)
    assert image.size == (2400, 3000)          # 8x10 inches at 300 dpi
    assert image.info["dpi"] == (300, 300)
    # A 3000x4000 photo (rotated from landscape) is 3:4, taller than 4:5, so it
    # gets side padding rather than top and bottom.
    assert image.getpixel((5, 1500)) == (255, 255, 255)
    assert image.getpixel((1200, 5)) != (255, 255, 255)


def test_portrait_is_left_alone():
    result = prepare(make_image(3000, 4000), Adjustments())
    assert open_result(result).size == (2400, 3000)


def test_rotation_choices():
    source = Image.open(io.BytesIO(make_image(400, 200)))
    assert render(source, Adjustments(rotate="none", fit="pad")).size == (2400, 3000)
    portrait = render(source, Adjustments(rotate="ccw"))
    assert portrait.size == (2400, 3000)


def test_pad_colour_is_configurable():
    result = prepare(make_image(4000, 3000), Adjustments(background="#000000"))
    assert open_result(result).getpixel((5, 1500)) == (0, 0, 0)


def test_crop_to_fill_leaves_no_padding():
    result = prepare(make_image(4000, 3000), Adjustments(fit="crop"))
    image = open_result(result)
    assert image.size == (2400, 3000)
    assert image.getpixel((5, 1500)) != (255, 255, 255)


def test_explicit_crop_rectangle_is_honoured():
    # Crop the bottom-right quarter of the (rotated) image and check that the
    # dark corner block, which lives top-left, is gone.
    adj = Adjustments.from_dict({"crop": {"x": 0.5, "y": 0.5, "w": 0.5, "h": 0.5}, "fit": "crop"})
    image = open_result(prepare(make_image(3000, 4000), adj))
    assert image.getpixel((10, 10)) != (20, 20, 20)


def test_no_enlarge_keeps_small_sources_at_native_size():
    adj = Adjustments(allow_enlarge=False)
    image = open_result(prepare(make_image(600, 800), adj))
    assert image.size == (2400, 3000)                 # canvas is still 8x10
    assert image.getpixel((5, 5)) == (255, 255, 255)  # but the photo is not blown up


def test_custom_size_and_dpi():
    adj = Adjustments(width_in=5, height_in=7, dpi=150)
    image = open_result(prepare(make_image(3000, 4000), adj))
    assert image.size == (750, 1050)
    assert image.info["dpi"] == (150, 150)


def test_png_and_tiff_round_trip():
    for fmt, expected in (("png", "PNG"), ("tiff", "TIFF")):
        adj = Adjustments(fmt=fmt, width_in=2, height_in=2.5, dpi=72)
        image = open_result(prepare(make_image(400, 300), adj))
        assert image.format == expected


def test_adjustments_reject_nonsense():
    with pytest.raises(ImagingError):
        Adjustments.from_dict({"rotate": "sideways"})
    with pytest.raises(ImagingError):
        Adjustments.from_dict({"background": "not-a-colour"})
    with pytest.raises(ImagingError):
        Adjustments.from_dict({"width_in": 500, "height_in": 500})
    with pytest.raises(ImagingError):
        Adjustments.from_dict({"crop": {"x": 0, "y": 0, "w": 0, "h": 1}})


def test_adjustments_layer_over_a_base():
    base = Adjustments(width_in=5, height_in=7, quality=80)
    merged = Adjustments.from_dict({"quality": 92}, base=base)
    assert (merged.width_in, merged.height_in, merged.quality) == (5, 7, 92)


def test_encode_sets_dpi_for_jpeg():
    image = Image.new("RGB", (100, 125), (10, 20, 30))
    assert open_result(encode(image, Adjustments(dpi=240))).info["dpi"] == (240, 240)


def test_heic_is_decoded_like_any_other_photo():
    """Phones shoot HEIC, and Pillow needs pillow-heif to read it."""
    landscape = prepare(make_image(1600, 1200, fmt="HEIF"), Adjustments())
    image = open_result(landscape)
    assert image.size == (2400, 3000)
    assert image.info["dpi"] == (300, 300)
    assert image.getpixel((5, 1500)) == (255, 255, 255)   # rotated, so padded at the sides


def test_heic_is_recognised_by_name_in_errors():
    heic = make_image(64, 64, fmt="HEIF")
    assert sniff_format(heic) in ("HEIC", "HEIF")
    assert sniff_format(b"\xff\xd8\xff\xe0rest") == "JPEG"
    assert sniff_format(b"II*\x00raw bytes") == "TIFF or camera raw"


def test_undecodable_bytes_raise_a_named_error():
    with pytest.raises(UnsupportedImageError) as exc:
        prepare(b"II*\x00 pretending to be a camera raw file", Adjustments())
    assert "camera raw" in str(exc.value)


# ---------- captions in the border ----------

from immich_print_prep.imaging import CAPTION_FONT_PATH, layout_caption  # noqa: E402

SHORT = "2026-07-04 14:30:05 CDT"
MEDIUM = SHORT + "\nFourth of July at the lake\nIn this photo: Alice, Bob"
LONG = " ".join(["word"] * 2000)
PORTRAIT_2X3 = (4000, 6000)       # also a landscape photo after the pipeline turns it
PAPER_8X10 = (2400, 3000)


def test_the_caption_font_ships_with_the_package():
    assert CAPTION_FONT_PATH.exists()


def test_a_short_caption_fits_the_natural_border_of_a_2x3_print():
    layout = layout_caption(PORTRAIT_2X3, PAPER_8X10, SHORT, 300)
    assert (layout.step, layout.edge) == ("centred", "right")
    assert layout.photo_box == (200, 0, 2000, 3000)             # full size, still centred


def test_a_landscape_photo_turned_onto_the_paper_gets_the_same_right_edge():
    layout = layout_caption(PORTRAIT_2X3, PAPER_8X10, SHORT, 300, turns=1)
    assert layout.edge == "right" and layout.photo_box[2:] == (2000, 3000)


def test_a_photo_turned_clockwise_puts_the_caption_on_the_left():
    assert layout_caption(PORTRAIT_2X3, PAPER_8X10, SHORT, 300, turns=3).edge == "left"


def test_a_longer_caption_slides_the_photo_before_anything_shrinks():
    layout = layout_caption(PORTRAIT_2X3, PAPER_8X10, MEDIUM, 300)
    left, top, width, height = layout.photo_box
    assert layout.step == "slid"
    assert (width, height) == (2000, 3000) and left < 200
    assert layout.strip > 200


def test_a_photo_already_shaped_like_the_print_shrinks_just_enough():
    layout = layout_caption(PAPER_8X10, PAPER_8X10, SHORT, 300)
    left, top, width, height = layout.photo_box
    assert layout.step == "shrunk"
    assert width < 2400 and height < 3000
    assert layout.strip == layout.edge_px + layout.gap_px + layout.line_height


def test_a_square_photo_is_captioned_along_the_bottom():
    layout = layout_caption((3000, 3000), PAPER_8X10, SHORT, 300)
    assert (layout.edge, layout.step) == ("bottom", "centred")
    assert layout.photo_box[2:] == (2400, 2400)


def test_an_overlong_caption_is_cut_off_rather_than_shrinking_the_photo_further():
    layout = layout_caption(PORTRAIT_2X3, PAPER_8X10, LONG, 300)
    assert layout.truncated and layout.lines[-1].endswith("…")
    assert layout.strip <= 450                                    # 1.5 inches at 300 dpi


def test_a_proof_wraps_the_caption_exactly_like_the_print():
    full = layout_caption(PORTRAIT_2X3, PAPER_8X10, MEDIUM, 300)
    assert full.scaled(0.25).lines == full.lines


def test_caption_text_lands_in_the_right_border_and_leaves_the_rest_alone():
    image = open_result(prepare(make_image(*PORTRAIT_2X3), Adjustments(), caption=SHORT))
    assert image.size == PAPER_8X10
    right = image.crop((2215, 60, 2395, 2940))
    left = image.crop((5, 60, 180, 2940))
    assert min(max(px) for px in right.getdata()) < 100          # dark grey text
    assert min(min(px) for px in left.getdata()) > 240           # untouched padding


def test_caption_text_is_light_on_a_dark_pad():
    image = open_result(
        prepare(make_image(*PORTRAIT_2X3), Adjustments(background="#101010"), caption=SHORT)
    )
    right = image.crop((2215, 60, 2395, 2940))
    assert max(min(px) for px in right.getdata()) > 180


def test_a_caption_forces_padding_even_in_crop_mode():
    image = open_result(prepare(make_image(*PORTRAIT_2X3), Adjustments(fit="crop"), caption=SHORT))
    assert min(min(px) for px in image.crop((5, 60, 180, 2940)).getdata()) > 240


def test_a_blank_caption_changes_nothing():
    captioned = open_result(prepare(make_image(*PORTRAIT_2X3), Adjustments(fit="crop"), caption="  "))
    plain = open_result(prepare(make_image(*PORTRAIT_2X3), Adjustments(fit="crop")))
    assert captioned.getpixel((5, 1500)) == plain.getpixel((5, 1500))


def test_caption_settings_round_trip_and_are_bounded():
    adj = Adjustments.from_dict({"caption": True, "caption_people": False, "caption_text": "x" * 5000})
    assert adj.caption and not adj.caption_people and len(adj.caption_text) == 1000
    assert Adjustments.from_dict(adj.to_dict()) == adj
    assert Adjustments.from_dict({"caption_text": None}, base=adj).caption_text is None
