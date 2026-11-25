"""临时上传的预览和确认提交。插件 API 始终只接受上传编号和角色资料。"""

import asyncio
import hashlib
import json
import re

from .media import font_name, reference_image, save_font

ROLE_FIELDS = {"character_id", "name", "keywords", "description", "attribution", "conversations"}
IMAGE_NOTICE = "参考图会随画图请求发给画图服务商。请勿登记真人照片。"
FONT_NOTICE = "请确认这个字体的许可证允许你这样使用。"


def character_key(role):
    if len(role) > 190:
        return "character.hash." + hashlib.sha256(role.encode()).hexdigest()
    return "character." + role


def role_record(entry, existing):
    if entry.keys() - ROLE_FIELDS - {"kind", "upload_id"}:
        raise ValueError("角色资料含有未知字段。")
    record = {
        **(existing or {"references": [], "keywords": []}),
        **{k: v for k, v in entry.items() if k in ROLE_FIELDS},
    }
    for key in ("name", "description"):
        if not isinstance(record.get(key), str) or not record[key].strip():
            raise ValueError("角色须有名字和外观描述。")
    for key in ("keywords", "conversations"):
        if key in record and not (
            isinstance(record[key], list)
            and all(isinstance(v, str) and v.strip() for v in record[key])
        ):
            raise ValueError("关键词和会话限制须为字符串列表。")
    if "attribution" in record and not isinstance(record["attribution"], str):
        raise ValueError("出处署名须为文字。")
    return record


async def read_upload(ctx, entry):
    key = entry.get("upload_id")
    if not isinstance(key, str) or not key:
        raise ValueError("需要有效的上传编号。")
    try:
        return await ctx.uploads.read(key)
    except (ValueError, KeyError, PermissionError, FileNotFoundError):
        raise ValueError("上传编号不可用或已过期。") from None


async def character(ctx, entry, commit):
    role = entry.get("character_id")
    if not isinstance(role, str) or role == "self" or not re.fullmatch("[a-z0-9-]+", role):
        raise ValueError("角色 ID 须由小写字母、数字或连字符组成，self 是保留名。")
    key = character_key(role)
    record = role_record(entry, await ctx.storage.get(key))
    description = f"角色 {record['name']}（{role}）。"
    if "upload_id" in entry:
        original = await read_upload(ctx, entry)
        (width, height), data = await asyncio.to_thread(reference_image, original)
        refs = record["references"]
        digest = hashlib.sha256(data).hexdigest()
        if any(ref["asset_id"] == digest for ref in refs):
            raise ValueError("已导入这张参考图。")
        if len(refs) >= 6:
            raise ValueError("每个角色最多 6 张参考图。")
        description += f"图片尺寸 {width}×{height}，已完整解码。" + IMAGE_NOTICE
        if commit:
            ref = await ctx.asset_writer.put(data, "image/png", "comic-maker")
            record["references"] = [*refs, ref.model_dump(mode="json")]
    if commit:
        await ctx.storage.set(key, record)
    return description


async def font(ctx, entry, commit):
    if entry.keys() - {"kind", "purpose", "upload_id"}:
        raise ValueError("字体资料含有未知字段。")
    purpose = entry.get("purpose")
    if purpose not in ("title", "caption", "sfx"):
        raise ValueError("字体用途须为 title、caption 或 sfx。")
    data = await read_upload(ctx, entry)
    name = await asyncio.to_thread(font_name, data)
    if commit:
        filename = hashlib.sha256(data).hexdigest() + ".font"
        await asyncio.to_thread(save_font, ctx.storage.path / "fonts", filename, data)
        await ctx.storage.set(
            "font." + purpose,
            {
                "filename": filename,
                "name": name,
                "license_confirmed_at": ctx.clock.now().isoformat(),
            },
        )
    return f"字体：{name}。用途：{purpose}。已包含用于检查的常用简体中文字符。" + FONT_NOTICE


async def process(ctx, entry, *, commit):
    if not isinstance(entry, dict) or entry.get("kind") not in ("character", "font"):
        raise ValueError("需要角色或字体导入资料。")
    handler = character if entry["kind"] == "character" else font
    message = await handler(ctx, entry, commit)
    if commit and "upload_id" in entry:
        await ctx.uploads.release(entry["upload_id"])
    return {"status": 200, "json": {"description": message}}


async def import_command(kind, args, api):
    cid = args["companion_id"]
    base = f"/api/v1/companions/{cid}/plugins/comic-maker/import/"
    entry = {k: v for k, v in args.items() if k != "companion_id"}
    entry["kind"] = kind
    preview = await api.request(
        "GET", base + "preview", query={"entry": json.dumps(entry, ensure_ascii=False)}
    )
    if not preview.json["ok"]:
        print((preview.json.get("result") or {}).get("description", "无法预览导入资料。"))
        return 1
    print(preview.json["result"]["description"])
    submitted = await api.request("POST", base + "commit", json=entry)
    if not submitted.json["ok"]:
        print((submitted.json.get("result") or {}).get("description", "未完成导入。"))
        return 1
    print("已保存。")
    return 0
