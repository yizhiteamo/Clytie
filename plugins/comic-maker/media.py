"""角色图检查与页眉页脚排版；阻塞操作由调用方送到工作线程。"""

import io
import os
import tempfile

from PIL import Image, ImageDraw, ImageFont

COMMON_CHINESE = "中国年月日第页漫画标题说明"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_FONT_BYTES = 30 * 1024 * 1024
FORMATS = ("PNG", "JPEG", "WEBP", "GIF")


def check_pixels(image):
    if image.width * image.height > 50_000_000:
        raise ValueError("图片超过 5000 万像素。")


def reference_image(data):
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError("参考图超过 20 MiB。")
    if data.startswith((b"GIF87a", b"GIF89a")) and not data.endswith(b";"):
        raise ValueError("GIF 文件不完整。")
    try:
        with Image.open(io.BytesIO(data), formats=FORMATS) as image:
            check_pixels(image)
            image.verify()
        with Image.open(io.BytesIO(data), formats=FORMATS) as image:
            size = image.size
            for index in range(image.n_frames if hasattr(image, "n_frames") else 1):
                image.seek(index)
                check_pixels(image)
                image.load()
                if index == 0:
                    with image.convert("RGBA") as first:
                        first.thumbnail((2048, 2048), Image.Resampling.LANCZOS)
                        first.info.clear()
                        output = io.BytesIO()
                        first.save(output, "PNG")
    except (OSError, SyntaxError, EOFError, Image.DecompressionBombError):
        raise ValueError("参考图格式不支持、像素过多或无法完整解码。") from None
    return size, output.getvalue()


def glyph(font, char):
    mask = font.getmask(char, mode="L")
    return mask.size, bytes(mask)


def font_name(data):
    if len(data) > MAX_FONT_BYTES:
        raise ValueError("字体超过 30 MiB。")
    if not data.startswith((b"\x00\x01\x00\x00", b"OTTO", b"true")):
        raise ValueError("需要可读取的 TTF 或 OTF 字体。")
    try:
        font = ImageFont.truetype(io.BytesIO(data), 32)
        missing = glyph(font, "\U0010ffff")
        for char in COMMON_CHINESE:
            shape = glyph(font, char)
            if shape == missing or not any(shape[1]):
                raise ValueError("字体缺少常用简体中文字符。")
        return " ".join(font.getname())
    except OSError:
        raise ValueError("字体无法读取。") from None


def save_font(directory, filename, data):
    directory.mkdir(exist_ok=True, parents=True)
    path = directory / filename
    if path.exists():
        return
    temporary = tempfile.NamedTemporaryFile(dir=directory, delete=False)
    try:
        with temporary:
            temporary.write(data)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary.name, path)
    finally:
        if os.path.exists(temporary.name):
            os.unlink(temporary.name)


def label(canvas, box, text, font_path):
    """按可用宽度缩字，长标题保留开头。"""
    draw = ImageDraw.Draw(canvas)
    width = box[2] - box[0]
    for size in (34, 28, 22, 18, 14):
        font = ImageFont.truetype(str(font_path), size)
        if font.getlength(text) <= width:
            break
    if font.getlength(text) > width:
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if font.getlength(text[:middle] + "…") <= width:
                low = middle
            else:
                high = middle - 1
        text = text[:low] + "…"
    draw.text((box[0], (box[1] + box[3]) // 2), text, fill="black", font=font, anchor="lm")


def typeset(data, title, number, date, footer, title_font, caption_font):
    width, height = 1024, 1536
    margin, bar = 24, 78
    with (
        Image.open(io.BytesIO(data)) as image,
        Image.new("RGB", (width, height), "white") as canvas,
    ):
        image.thumbnail((width, height - 2 * bar), Image.Resampling.LANCZOS)
        with image.convert("RGBA") as source:
            canvas.paste(
                source, ((width - source.width) // 2, (height - source.height) // 2), source
            )
        label(canvas, (margin, 0, width - 200, bar), title, title_font)
        label(canvas, (width - 175, 0, width - margin, bar), f"第 {number} 页", caption_font)
        label(
            canvas,
            (margin, height - bar, width - margin, height),
            "　".join(part for part in (date, footer) if part),
            caption_font,
        )
        output = io.BytesIO()
        canvas.save(output, "PNG")
        return output.getvalue()
