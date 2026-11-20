from io import BytesIO

import pytest
from PIL import Image, PngImagePlugin

from clytie.agent.vision import normalize_image


def test_reference_png_removes_exif_and_text():
    text = PngImagePlugin.PngInfo()
    text.add_text("Comment", "fictional metadata")
    exif = Image.Exif()
    exif[270] = "fictional description"
    with Image.new("RGBA", (3, 2), (80, 40, 20, 100)) as image, BytesIO() as source:
        image.save(source, format="PNG", pnginfo=text, exif=exif)
        data = source.getvalue()
        expected = image.tobytes()
    with Image.open(BytesIO(data)) as original:
        assert original.text["Comment"] == "fictional metadata"
        assert original.getexif()[270] == "fictional description"
    result = normalize_image(data, len(data), clean_png=True)
    with Image.open(BytesIO(result.data), formats=["PNG"]) as clean:
        assert clean.tobytes() == expected
        assert not clean.getexif() and not clean.text


@pytest.mark.parametrize("fmt", ["GIF", "WEBP"])
def test_reference_animation_becomes_one_png_frame(fmt):
    with Image.new("RGB", (4, 4), "red") as red, Image.new("RGB", (4, 4), "blue") as blue:
        with BytesIO() as stream:
            red.save(stream, format=fmt, save_all=True, append_images=[blue])
            data = stream.getvalue()
    with Image.open(BytesIO(data)) as source:
        expected = source.convert("RGBA").tobytes()
    result = normalize_image(data, len(data), clean_png=True)
    with Image.open(BytesIO(result.data), formats=["PNG"]) as clean:
        assert clean.n_frames == 1 and clean.tobytes() == expected
