import asyncio
from io import BytesIO
from threading import Event

import pytest
from PIL import Image

from clytie.agent import vision
from clytie.agent.vision import normalize_image
from clytie.sdk.testing import MockClock
from clytie.sdk.types import MediaContent


def gif(*, animated):
    with Image.new("RGB", (2, 2), "red") as red, Image.new("RGB", (2, 2), "blue") as blue:
        with BytesIO() as output:
            red.save(
                output, format="GIF", save_all=animated, append_images=[blue] if animated else []
            )
            return output.getvalue()


def test_animated_gif_uses_first_frame():
    result = normalize_image(gif(animated=True), 4096)
    assert result.first_frame and result.media_type == "image/png"
    with Image.open(BytesIO(result.data), formats=["PNG"]) as first:
        assert first.getpixel((0, 0)) == (255, 0, 0, 255)


def test_static_gif_preserves_original_bytes():
    raw = gif(animated=False)
    result = normalize_image(raw, 4096)
    assert result.data == raw and result.media_type == "image/gif" and not result.first_frame


def test_gif_pixel_limit_precedes_decoding():
    raw = bytearray(gif(animated=False))
    raw[6:10] = (8000).to_bytes(2, "little") * 2
    with pytest.raises(ValueError, match="failed"):
        normalize_image(bytes(raw), 4096)


def test_damaged_gif_rejected():
    with pytest.raises(ValueError, match="failed"):
        normalize_image(gif(animated=False)[:-4], 4096)


async def test_gif_decoder_timeout_retains_slots_until_threads_exit(monkeypatch):
    clock = MockClock()
    fetcher = vision.ImageFetcher(clock, 4096)
    entered = asyncio.Queue()
    release = Event()
    loop = asyncio.get_running_loop()
    calls = []
    raw = gif(animated=True)

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
