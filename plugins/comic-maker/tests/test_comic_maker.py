import hashlib
import importlib.util
import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image, ImageFont, PngImagePlugin

from clytie.sdk import CancelToken
from clytie.sdk.testing import FakeContext, MockClock
from clytie.sdk.testing.samples import sample_conversation, sample_invocation
from clytie.sdk.types import Denial, ToolCall

ROOT = Path(__file__).parents[1]


@pytest.fixture
async def comic(tmp_path):
    spec = importlib.util.spec_from_file_location(
        "fictional_comic", ROOT / "plugin.py", submodule_search_locations=[str(ROOT)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    ctx = FakeContext(root=tmp_path, clock=MockClock(), permissions=["assets.write"])
    ctx.uploads = Uploads()
    ctx.appearance = SimpleNamespace(current=empty_appearance)
    renderer = Renderer(ctx)
    ctx.services = {"image.render": renderer}
    plugin = module.Plugin()
    await plugin.start(ctx)
    return plugin, ctx, renderer


async def empty_appearance():
    return SimpleNamespace(description="", references=[])


def png(color="blue", size=(24, 36)):
    output = io.BytesIO()
    with Image.new("RGB", size, color) as img:
        meta = PngImagePlugin.PngInfo()
        meta.add_text("fictional", "strip this metadata")
        img.save(output, format="PNG", pnginfo=meta)
    return output.getvalue()


class Renderer:
    def __init__(self, ctx):
        self.ctx, self.requests, self.fail_on = ctx, [], 0

    async def call(self, method, args):
        assert method == "render"
        self.requests.append(args)
        if self.fail_on == len(self.requests):
            return {"error": "rate_limited"}
        data = png("blue" if len(self.requests) == 1 else "red")
        ref = await self.ctx.asset_writer.put(data, "image/png", "image-gen")
        return {"asset": ref.model_dump(mode="json")}


class Uploads(dict):
    async def read(self, key):
        return self[key]

    async def release(self, key):
        self.pop(key)


def panel(characters=()):
    return {
        "characters": list(characters),
        "scene": "虚构花园里开出一朵蓝花",
        "dialogue": [{"speaker": role, "text": "花开了"} for role in characters],
        "sfx": "叮",
    }


def arguments(*panels):
    return {"title": "蓝花", "pages": [{"panels": [p]} for p in panels]}


async def draw(plugin, args):
    tool, inv = plugin.create_draw(), sample_invocation()
    call = ToolCall(call_id="fake-comic", tool="draw_comic", arguments=args)
    plan = await tool.prepare(call, inv)
    if isinstance(plan, Denial):
        return plan
    return await tool.execute(plan, inv, CancelToken())


def character(**overrides):
    return {
        "character_id": "sprout",
        "name": "嫩芽",
        "description": "蓝色花冠",
        "keywords": ["花店"],
        **overrides,
    }


async def commit(plugin, entry):
    return await plugin.import_commit({"json": entry})


@pytest.mark.parametrize(
    ("modify", "reason"),
    [
        (lambda a: a["pages"].extend([a["pages"][-1]] * 9), "第 11 页"),
        (lambda a: a["pages"][1]["panels"].extend([panel()] * 10), "第 2 页"),
        (
            lambda a: a["pages"][1]["panels"][0].update(
                dialogue=[{"speaker": "self", "text": "花" * 31}]
            ),
            "第 2 页",
        ),
        (lambda a: a["pages"][1].update(panels=[]), "第 2 页"),
        (lambda a: a["pages"][1]["panels"][0].update(scene=" "), "第 2 页"),
        (lambda a: a["pages"][1]["panels"][0].update(characters=["unknown"]), "未知角色"),
        (lambda a: a["pages"][1]["panels"][0].update(characters=["self"]), "形象"),
        (lambda a: a["pages"][1]["panels"][0].update(characters=["sprout"]), "会话"),
    ],
)
async def test_invalid_later_page_never_renders(comic, modify, reason):
    plugin, _, renderer = comic
    await commit(plugin, character(kind="character", conversations=["private_test"]))
    args = arguments(panel(), panel())
    modify(args)
    result = await draw(plugin, args)
    message = result.message_for_model if isinstance(result, Denial) else result.content
    assert reason in message and "页" in message
    assert not renderer.requests


async def test_page_assets_prompt_previous_page_and_no_dialogue_in_result(comic):
    plugin, ctx, renderer = comic
    ctx.uploads["art"] = png("green")
    response = await commit(plugin, character(kind="character", upload_id="art"))
    assert response["status"] == 200
    assert "art" not in ctx.uploads
    args = arguments(panel(["sprout"]), panel(["sprout"]))
    args["style"] = "纸雕画风"
    result = await draw(plugin, args)
    assert result.status == "ok" and len(result.assets) == 2
    first, second = renderer.requests
    assert first["size"] == "1024x1536"
    assert second["references"] == [result.assets[0].model_dump(mode="json"), *first["references"]]
    assert len(first["references"]) == 1
    assert all(word in first["prompt"] for word in ["纸雕画风", "蓝色花冠", "蓝花", "花开了", "叮"])
    assert (
        "第 2 页" in result.content and "嫩芽" in result.content and "花开了" not in result.content
    )
    assert result.assets[0].asset_id == hashlib.sha256(png("blue")).hexdigest()
    assert result.assets[1].asset_id == hashlib.sha256(png("red")).hexdigest()


async def test_no_partial_assets_after_failure(comic):
    plugin, _, renderer = comic
    renderer.fail_on = 2
    result = await draw(plugin, arguments(panel(), panel(), panel()))
    assert result.status == "error" and not result.assets
    assert "第 2 页" in result.content and len(renderer.requests) == 2


async def test_invalid_second_render_omits_first_page(comic):
    plugin, ctx, renderer = comic

    async def render(method, args):
        response = await renderer.call(method, args)
        return response if len(renderer.requests) == 1 else {}

    ctx.services["image.render"] = SimpleNamespace(call=render)
    result = await draw(plugin, arguments(panel(), panel()))
    assert result.status == "error" and not result.assets and "第 2 页" in result.content


async def test_reference_previews_revalidate_and_strip_metadata(comic):
    plugin, ctx, _ = comic
    ctx.uploads["art"] = png(size=(2100, 210))
    entry = character(kind="character", upload_id="art")

    async def preview():
        return await plugin.import_preview({"query": {"entry": json.dumps(entry)}})

    response = await preview()
    assert response["status"] == 200
    assert (
        "画图服务商" in response["json"]["description"]
        and "2100" in response["json"]["description"]
    )
    assert await ctx.storage.keys() == []
    ctx.uploads["art"] = b"not an image"
    assert (await commit(plugin, entry))["status"] == 400
    assert await ctx.storage.keys() == []
    ctx.uploads["art"] = png(size=(2100, 210))
    assert (await commit(plugin, entry))["status"] == 200
    role = await ctx.storage.get("character.sprout")
    data = await ctx.assets.read(role["references"][0]["asset_id"])
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (2048, 205) and not image.info


async def test_context_roster_visibility_and_budget(comic):
    plugin, _, _ = comic
    await commit(plugin, character(kind="character"))
    await commit(
        plugin,
        character(
            kind="character", character_id="secret", name="隐藏角色", conversations=["other"]
        ),
    )
    req = SimpleNamespace(conversation=sample_conversation(), has_appearance=True, budget_chars=300)
    section = await plugin.contribute(req)
    assert all(word in section.text for word in ["self", "嫩芽", "sprout", "花店"])
    assert "隐藏" not in section.text
    await commit(plugin, character(kind="character", keywords=["花" * 310]))
    section = await plugin.contribute(req)
    assert len(section.text) <= 300 and "嫩芽" in section.text
    assert "花" not in section.text and "sprout" not in section.text


async def test_font_rejects_missing_chinese_and_invalid_files(comic):
    plugin, ctx, _ = comic
    for data in (ImageFont.load_default().font_bytes, b"not a font", b"x" * (30 * 1024 * 1024 + 1)):
        ctx.uploads["font"] = data
        entry = {"kind": "font", "purpose": "title", "upload_id": "font"}
        response = await plugin.import_preview({"query": {"entry": json.dumps(entry)}})
        assert response["status"] == 400
        assert (await commit(plugin, entry))["status"] == 400
    assert await ctx.storage.keys() == []


def test_manifest_tool_constraints_and_confirmation():
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    tool = manifest["contributes"]["tools"][0]
    assert tool["name"] == "draw_comic" and tool["heavy"]
    assert all(word in tool["description"] for word in ("短", "真人", "角色表", "self", "来源"))
    section = manifest["contributes"]["context_section"]
    assert section["budget_chars"] == 300 and section["when_tool"] == "draw_comic"
    commit_route = next(r for r in manifest["contributes"]["api_routes"] if r["method"] == "POST")
    assert commit_route["confirm"] and commit_route["admin_password"]


async def test_offscreen_narration_and_long_character_ids(comic):
    plugin, _, renderer = comic
    role_id = "a" * 202
    response = await commit(plugin, character(kind="character", character_id=role_id))
    assert response["status"] == 200
    scene = panel([role_id])
    scene["dialogue"] = [{"speaker": "旁白", "text": "蓝花开了"}]
    result = await draw(plugin, arguments(scene))
    assert result.status == "ok" and "蓝色花冠" in renderer.requests[0]["prompt"]


@pytest.mark.parametrize("role", ["self", "NotValid", "../bad"])
async def test_reserved_and_invalid_ids_refused(comic, role):
    plugin, _, _ = comic
    assert (await commit(plugin, character(kind="character", character_id=role)))["status"] == 400


async def test_self_uses_current_order_and_result_names_missing_picture(comic):
    plugin, ctx, renderer = comic
    refs = [
        await ctx.asset_writer.put(png(color), "image/png", "fictional")
        for color in ("red", "blue")
    ]

    async def current():
        return SimpleNamespace(description="月牙形花冠", references=refs)

    ctx.appearance = SimpleNamespace(current=current)
    await commit(plugin, character(kind="character"))
    result = await draw(plugin, arguments(panel(["sprout", "self"])))
    assert result.status == "ok"
    assert renderer.requests[0]["references"] == [ref.model_dump(mode="json") for ref in refs]
    assert "月牙形花冠" in renderer.requests[0]["prompt"]
    assert "未附参考图：sprout" in result.content


async def test_duplicate_reference_six_limit_and_protected_metadata(comic):
    plugin, ctx, _ = comic
    first = character(kind="character", conversations=["test_group"], upload_id="first")
    ctx.uploads["first"] = png("red")
    assert (await commit(plugin, first))["status"] == 200
    minimal = {"kind": "character", "character_id": "sprout", "upload_id": "more"}
    ctx.uploads["more"] = png("red")
    assert (await commit(plugin, minimal))["status"] == 400
    for index in range(5):
        ctx.uploads["more"] = png((30, 40, index))
        assert (await commit(plugin, minimal))["status"] == 200
    ctx.uploads["more"] = png("green")
    assert (await commit(plugin, minimal))["status"] == 400
    record = await ctx.storage.get("character.sprout")
    assert len(record["references"]) == 6 and record["conversations"] == ["test_group"]


def test_oversize_pixels_bytes_and_truncated_images(comic_media):
    import struct
    import zlib

    # Change only the IHDR size and CRC to exercise the pixel gate before decode.
    data = bytearray(png())
    data[16:24] = struct.pack(">II", 10_000, 5_001)
    data[29:33] = struct.pack(">I", zlib.crc32(data[12:29]))
    for invalid in (bytes(data), png()[:40], b"x" * (20 * 1024 * 1024 + 1)):
        with pytest.raises(ValueError):
            comic_media.reference_image(invalid)


@pytest.fixture
def comic_media():
    spec = importlib.util.spec_from_file_location("fictional_media", ROOT / "media.py")
    media = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(media)
    return media


def fictional_font():
    """Map test Chinese characters to a visible glyph of Pillow's in-memory fixture font."""
    import string
    import struct

    data = bytearray(ImageFont.load_default().font_bytes)
    count = int.from_bytes(data[4:6], "big")
    for offset in range(12, 12 + 16 * count, 16):
        if data[offset : offset + 4] == b"cmap":
            break
    else:
        raise AssertionError("fixture font lacks a cmap")
    codes = sorted({ord(c) for c in "中国年月日第页漫画标题说明蓝花例-" + string.digits})
    groups = b"".join(struct.pack(">III", code, code, 4) for code in codes)
    subtable = struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, len(codes)) + groups
    cmap = struct.pack(">HHHHI", 0, 1, 3, 10, 12) + subtable
    data.extend(b"\0" * (-len(data) % 4))
    checksum = (
        sum(int.from_bytes(cmap[i : i + 4], "big") for i in range(0, len(cmap), 4)) & 0xFFFFFFFF
    )
    data[offset + 4 : offset + 16] = struct.pack(">III", checksum, len(data), len(cmap))
    data.extend(cmap)
    return bytes(data)


async def test_font_import_license_private_storage_and_header_footer(comic):
    plugin, ctx, _ = comic
    font = fictional_font()
    for purpose in ["title", "caption"]:
        ctx.uploads["font"] = font
        entry = {"kind": "font", "upload_id": "font", "purpose": purpose}
        preview = await plugin.import_preview({"query": {"entry": json.dumps(entry)}})
        assert preview["status"] == 200
        assert all(s in preview["json"]["description"] for s in (purpose, "许可证", "简体中文"))
        assert (await commit(plugin, entry))["status"] == 200
        saved = await ctx.storage.get("font." + purpose)
        assert saved["license_confirmed_at"] == ctx.clock.now().isoformat()
        assert (ctx.storage.path / "fonts" / saved["filename"]).read_bytes() == font
        assert "font" not in ctx.uploads
    result = await draw(plugin, arguments(panel()))
    data = await ctx.assets.read(result.assets[0].asset_id)
    assert data != png("blue")
    with Image.open(io.BytesIO(data)) as image:
        assert image.size == (1024, 1536)
        for region in [(0, 0, 1024, 78), (0, 1458, 1024, 1536)]:
            assert image.crop(region).convert("L").getextrema() == (0, 255)
    ctx.config = {"header_footer": False}
    result = await draw(plugin, arguments(panel()))
    assert await ctx.assets.read(result.assets[0].asset_id) == png("red")


async def test_cli_shows_preview_then_commits_upload_id(comic, capsys):
    from clytie.sdk.types import ApiResponse

    plugin, _, _ = comic
    seen = []

    class Api:
        async def request(self, method, path, **kwargs):
            seen.append((method, path, kwargs))
            if method == "POST":
                assert "字体许可证" in capsys.readouterr().out
            return ApiResponse(
                status=200, json={"ok": True, "result": {"description": "字体许可证"}}
            )

    result = await plugin.font_import(
        {"companion_id": "companion_a", "upload_id": "demo", "purpose": "title"}, Api()
    )
    assert result == 0
    assert [row[0] for row in seen] == ["GET", "POST"]
    assert seen[1][2]["json"] == json.loads(seen[0][2]["query"]["entry"])
