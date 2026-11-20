import asyncio
from io import BytesIO
from random import Random
from threading import Event

import pytest
from PIL import Image

from clytie.agent import vision
from clytie.agent.vision import normalize_image
from clytie.sdk.testing import MockClock
from clytie.sdk.types import MediaContent


def encoded_image(fmt, *, animated=False, color="red"):
    with Image.new("RGB", (2, 2), color) as red, Image.new("RGB", (2, 2), "blue") as blue:
        with BytesIO() as output:
            red.save(
                output,
                format=fmt,
                **({"save_all": True, "append_images": [blue]} if animated else {}),
            )
            return output.getvalue()


@pytest.mark.parametrize("fmt", ["GIF", "WEBP"])
def test_animated_image_uses_first_frame(fmt):
    result = normalize_image(encoded_image(fmt, animated=True), 4096)
    assert result.first_frame and result.media_type == "image/png"
    with Image.open(BytesIO(result.data), formats=["PNG"]) as first:
        r, g, b, alpha = first.getpixel((0, 0))
        assert r > 240 and g < 10 and b < 10 and alpha == 255


@pytest.mark.parametrize("fmt", ["PNG", "JPEG", "GIF", "WEBP"])
def test_static_image_preserves_original_bytes(fmt):
    raw = encoded_image(fmt)
    result = normalize_image(raw, 4096)
    assert (
        result.data == raw
        and result.media_type == "image/" + fmt.lower()
        and not result.first_frame
    )


def test_gif_pixel_limit_precedes_decoding():
    raw = bytearray(encoded_image("GIF", animated=False))
    raw[6:10] = (8000).to_bytes(2, "little") * 2
    with pytest.raises(ValueError, match="failed"):
        normalize_image(bytes(raw), 4096)


def test_damaged_gif_rejected():
    with pytest.raises(ValueError, match="failed"):
        normalize_image(encoded_image("GIF", animated=False)[:-4], 4096)


@pytest.fixture(params=["JPEG-half", "PNG-half", "PNG-crc", "WEBP-half"])
def damaged_media(request):
    fmt, damage = request.param.split("-")
    size = (96, 96)
    pixels = Random(42).randbytes(size[0] * size[1] * 3)
    with Image.frombytes("RGB", size, pixels) as image, BytesIO() as output:
        image.save(output, format=fmt)
        raw = bytearray(output.getvalue())

    if damage == "crc":
        # Pillow emits one IDAT for this small RGB fixture. Flip only its checksum.
        chunk = raw.index(b"IDAT")
        length = int.from_bytes(raw[chunk - 4 : chunk], "big")
        raw[chunk + 4 + length] ^= 1
        with Image.open(BytesIO(raw), formats=[fmt]) as image:
            assert image.tobytes() == pixels
        with Image.open(BytesIO(raw), formats=[fmt]) as image, pytest.raises(Exception):
            image.verify()
    else:
        raw = raw[: len(raw) // 2]
        if fmt in {"JPEG", "PNG"}:
            with Image.open(BytesIO(raw), formats=[fmt]) as image:
                assert image.size == size
                with pytest.raises(Exception):
                    image.load()
    return MediaContent(data=bytes(raw), media_type="image/" + fmt.lower())


def test_normalize_rejects_damaged_media(damaged_media):
    with pytest.raises(Exception):
        normalize_image(damaged_media.data, len(damaged_media.data))


async def test_fetch_reports_failed_for_damaged_media(damaged_media):
    class Channel:
        async def fetch_media(self, part, cancel):
            return damaged_media

    fetcher = vision.ImageFetcher(MockClock(), len(damaged_media.data))
    try:
        assert await fetcher.fetch(Channel(), None) == ("failed", None)
    finally:
        await asyncio.gather(*fetcher.workers)


async def test_gif_decoder_timeout_retains_slots_until_threads_exit(monkeypatch):
    clock = MockClock()
    fetcher = vision.ImageFetcher(clock, 4096)
    entered = asyncio.Queue()
    release = Event()
    loop = asyncio.get_running_loop()
    calls = []
    raw = encoded_image("GIF", animated=True)

    class Channel:
        async def fetch_media(self, part, cancel):
            calls.append(part)
            return MediaContent(data=raw, media_type="image/jpeg")

    def slow_decode(data, limit):
        loop.call_soon_threadsafe(entered.put_nowait, True)
        assert release.wait(5)
        return normalize_image(data, limit)

    monkeypatch.setattr(vision, "normalize_image", slow_decode)
    tasks = [asyncio.create_task(fetcher.fetch(Channel(), i)) for i in range(3)]
    try:
        await entered.get()
        await entered.get()
        clock.advance(15)
        assert await tasks[0] == await tasks[1] == ("failed", None)
        assert calls == [0, 1]
    finally:
        release.set()
    status, image = await tasks[2]
    assert status == "ok" and image.first_frame and image.media_type == "image/png"
    await asyncio.gather(*fetcher.workers)


@pytest.mark.parametrize("fmt", ["JPEG", "PNG", "WEBP", "GIF"])
def test_pixel_limit_rejects_before_load(fmt, monkeypatch):
    raw = encoded_image(fmt)
    with Image.open(BytesIO(raw), formats=[fmt]) as image:
        decoder_type = type(image)
    monkeypatch.setattr(vision, "MAX_IMAGE_PIXELS", 3)
    monkeypatch.setattr(decoder_type, "load", lambda *_: pytest.fail("decoded oversized image"))
    with pytest.raises(ValueError, match="failed"):
        normalize_image(raw, 4096)
