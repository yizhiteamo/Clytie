"""Normalize inbound image bytes before publication to the asset store."""

import asyncio
from dataclasses import dataclass
from io import BytesIO

from PIL import Image

from clytie.plugin_host.http import ResponseTooLarge, media_download_limit
from clytie.sdk import CancelToken

MAX_IMAGE_PIXELS = 50_000_000


class ImageFetcher:
    """One instance per companion. Timed-out decoders retain their slot until exit."""

    def __init__(self, clock, max_bytes):
        self.clock = clock
        self.max_bytes = max_bytes
        self.slots = asyncio.Semaphore(2)
        self.workers = set()

    async def fetch(self, channel, part):
        await self.slots.acquire()
        cancel = CancelToken()
        binding = media_download_limit.set(self.max_bytes)
        try:
            download = asyncio.create_task(channel.fetch_media(part, cancel))
        finally:
            media_download_limit.reset(binding)
        timer = asyncio.create_task(self.clock.sleep(15))
        decoder = None
        try:
            done, _ = await asyncio.wait((download, timer), return_when=asyncio.FIRST_COMPLETED)
            if timer in done:
                raise TimeoutError
            content = download.result()
            decoder = asyncio.create_task(
                asyncio.to_thread(normalize_image, content.data, self.max_bytes)
            )
            done, _ = await asyncio.wait((decoder, timer), return_when=asyncio.FIRST_COMPLETED)
            if timer in done:
                raise TimeoutError
            return "ok", decoder.result()
        except ResponseTooLarge:
            return "too_large", None
        except ValueError as exc:
            code = exc.args[0] if exc.args else None
            return (code if code in ("too_large", "unsupported") else "failed"), None
        except Exception:
            return "failed", None
        finally:
            cancel.cancel()
            download.cancel()
            timer.cancel()
            cleanup = asyncio.create_task(self._release(download, timer, decoder))
            self.workers.add(cleanup)
            cleanup.add_done_callback(self.workers.discard)

    async def _release(self, download, timer, decoder):
        pending = [download, timer]
        if decoder is not None:
            pending.append(decoder)
        try:
            await asyncio.gather(*pending, return_exceptions=True)
        finally:
            self.slots.release()


@dataclass(frozen=True)
class InboundImage:
    data: bytes
    media_type: str
    first_frame: bool = False


REFERENCE_ATTACHMENT_BYTES = 20 * 1024 * 1024


def normalize_image(data: bytes, max_bytes: int, *, clean_png: bool = False) -> InboundImage:
    """Run in the fetch worker. Validate pixels before publishing any inbound asset."""
    if len(data) > max_bytes:
        raise ValueError("too_large")
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        fmt = "PNG"
    elif data.startswith(b"\xff\xd8\xff"):
        fmt = "JPEG"
    elif data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        fmt = "WEBP"
    elif data.startswith((b"GIF87a", b"GIF89a")) and data.endswith(b";"):
        fmt = "GIF"
    else:
        raise ValueError("failed")

    if fmt == "PNG":
        # verify checks chunk checksums; load below checks the compressed pixels.
        with Image.open(BytesIO(data), formats=[fmt]) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("too_many_pixels" if clean_png else "failed")
            image.verify()
    with Image.open(BytesIO(data), formats=[fmt]) as image:
        first_frame = None
        index = 0
        while True:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise ValueError("too_many_pixels" if clean_png else "failed")
            image.load()
            if index == 0:
                animated = fmt in {"GIF", "WEBP"} and image.is_animated
            if index == 0 and (clean_png or animated):
                with image.convert("RGBA") as first, BytesIO() as output:
                    if clean_png:
                        first.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                        first.info.clear()
                    first.save(output, format="PNG")
                    if clean_png:
                        # Even an incompressible 2048² RGBA image fits the model attachment budget.
                        assert output.tell() <= REFERENCE_ATTACHMENT_BYTES, "unexpected PNG size"
                    if not clean_png and output.tell() > max_bytes:
                        raise ValueError("too_large")
                    first_frame = output.getvalue()
            index += 1
            try:
                image.seek(index)
            except EOFError:
                break
        if first_frame is not None:
            return InboundImage(first_frame, "image/png", first_frame=animated)
    return InboundImage(data, "image/" + fmt.lower())
