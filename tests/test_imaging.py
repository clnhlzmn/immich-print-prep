"""The conversion pipeline: the part that has to match the XnView preset."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from immich_print_prep.imaging import Adjustments, ImagingError, encode, prepare, render

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
