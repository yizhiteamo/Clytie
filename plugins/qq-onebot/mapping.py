"""OneBot v11 消息段与 SDK 消息的离线映射。"""

import base64
import hashlib
import json
import re
from datetime import datetime, timezone
from uuid import uuid4

from clytie.sdk.types import (
    AudioPart,
    EventEnvelope,
    FilePart,
    ImagePart,
    MentionPart,
    Message,
    MessageRef,
    QuotePart,
    StickerPart,
    TextPart,
    UnknownPart,
    VideoPart,
)


def _number(value, *, signed=False):
    if type(value) is int:
        value = str(value)
    pattern = r"-?[0-9]+" if signed else r"[0-9]+"
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def _raw(data):
    if not isinstance(data, dict):
        return None
    try:
        raw = json.dumps(data, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return raw if len(raw.encode("utf-8")) <= 4096 else None
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return None


def _inbound_part(ctx, segment, expected, reference):
    if not isinstance(segment, dict) or not isinstance(segment.get("type"), str):
        return UnknownPart(raw_type="unknown"), 1
    kind, data = segment["type"], segment.get("data")
    if kind in {"file", "mface"}:
        raw = _raw(data)
        if kind == "file":
            return FilePart(raw_ref=raw), int(raw is None)
        summary = data.get("summary") if isinstance(data, dict) else None
        identifier = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16] if raw else "unknown"
        return StickerPart(
            sticker_id=f"mface:{identifier}",
            raw_ref=raw,
            summary=summary[:100] if isinstance(summary, str) else None,
        ), int(raw is None)
    unknown = UnknownPart(raw_type=kind)
    if not isinstance(data, dict):
        return unknown, 1
    if kind == "text" and isinstance(data.get("text"), str):
        return TextPart(text=data["text"]), 0
    if kind == "at":
        account = "all" if data.get("qq") == "all" else _number(data.get("qq"))
        if account is not None:
            return MentionPart(
                actor_id=ctx.identity.resolve_actor(account).actor_id, is_self=account == expected
            ), 0
    if kind in {"image", "record", "video"}:
        raw = data.get("url")
        if not isinstance(raw, str) or not raw:
            raw = data.get("file")
        if isinstance(raw, str) and raw:
            cls = {"image": ImagePart, "record": AudioPart, "video": VideoPart}[kind]
            return cls(raw_ref=raw), 0
    if kind == "reply":
        identifier = _number(data.get("id"), signed=True)
        if identifier is not None:
            return QuotePart(
                message_ref=reference.model_copy(update={"platform_message_id": identifier})
            ), 0
    if kind == "face":
        identifier = _number(data.get("id"))
        if identifier is not None:
            return StickerPart(sticker_id=f"face:{identifier}"), 0
    return unknown, int(kind in {"text", "at", "image", "record", "video", "reply", "face"})


def normalize_event(ctx, payload, expected_account):
    """丢弃不属于本账号的事件；畸形段单独计数，不下载媒体。"""
    if (
        isinstance(payload, dict)
        and payload.get("post_type") == "notice"
        and payload.get("notice_type") in {"group_increase", "group_decrease"}
    ):
        group = _number(payload.get("group_id"))
        actor = _number(payload.get("user_id"))
        if _number(payload.get("self_id")) != expected_account or group is None or actor is None:
            return None, 1
        conversation = ctx.identity.resolve_conversation("group", group)
        return EventEnvelope(
            event_id=f"{conversation.channel_id}:{uuid4().hex}",
            dedupable=False,
            type="conversation.notice",
            companion_id=ctx.companion_id,
            channel_id=conversation.channel_id,
            conversation=conversation,
            notice={
                "kind": "member_join"
                if payload["notice_type"] == "group_increase"
                else "member_leave",
                "actor_id": ctx.identity.resolve_actor(actor).actor_id,
            },
            received_at=ctx.clock.now(),
        ), 0
    if not isinstance(payload, dict) or payload.get("post_type") != "message":
        return None, 0
    kind = payload.get("message_type")
    if kind not in ("private", "group") or not isinstance(payload.get("message"), list):
        return None, 0
    account = _number(payload.get("self_id"))
    sender = _number(payload.get("user_id"))
    identifier = _number(payload.get("message_id"), signed=True)
    platform_id = _number(payload.get("group_id")) if kind == "group" else sender
    timestamp = payload.get("time")
    if (
        account != expected_account
        or sender is None
        or sender == expected_account
        or identifier is None
        or platform_id is None
        or type(timestamp) is not int
    ):
        return None, 0
    try:
        sent_at = datetime.fromtimestamp(timestamp, timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None, 0
    channel = ctx.identity.resolve_actor(expected_account).channel_id
    conversation = ctx.identity.resolve_conversation(kind, platform_id)
    reference = MessageRef(
        channel_id=channel,
        conversation_id=conversation.conversation_id,
        platform_message_id=identifier,
    )
    parts, count = [], 0
    for segment in payload["message"]:
        part, malformed = _inbound_part(ctx, segment, expected_account, reference)
        parts.append(part)
        count += malformed
    return EventEnvelope(
        event_id=f"{channel}:{identifier}",
        type="conversation.message",
        companion_id=ctx.companion_id,
        channel_id=channel,
        conversation=conversation,
        message=Message(
            ref=reference,
            sender=ctx.identity.resolve_actor(sender),
            parts=parts,
            sent_at=sent_at,
            reply_to=next(
                (part.message_ref for part in parts if isinstance(part, QuotePart)), None
            ),
        ),
        received_at=ctx.clock.now(),
    ), count


async def encode_parts(ctx, intent, media_limits, max_images_per_message=1):
    """完整编码后才交给发送器；无法编码的任一段会拒绝整个意图。"""
    segments = []
    image_count = image_bytes = 0
    parts = intent.parts
    if parts and intent.kind == "reply" and intent.target is not None:
        parts = [QuotePart(message_ref=intent.target), *parts]
    for part in parts:
        kind = part.type
        if kind == "text":
            data = {"text": part.text}
        elif kind == "mention":
            account = (
                ctx.identity.self_account()
                if part.is_self
                else ctx.identity.account_for(part.actor_id)
            )
            if _number(account) is None:
                raise ValueError("invalid_request")
            kind, data = "at", {"qq": account}
        elif kind == "quote":
            ref = part.message_ref
            if (
                ref.channel_id != intent.channel_id
                or ref.conversation_id != intent.conversation_id
                or _number(ref.platform_message_id, signed=True) is None
            ):
                raise ValueError("invalid_request")
            kind, data = "reply", {"id": ref.platform_message_id}
        elif kind == "sticker" and part.sticker_id.startswith("face:"):
            identifier = _number(part.sticker_id[5:])
            if identifier is None:
                raise ValueError("invalid_request")
            kind, data = "face", {"id": identifier}
        elif kind in {"image", "audio", "video"}:
            if part.asset is None:
                raise ValueError("invalid_request")
            try:
                info = await ctx.assets.info(part.asset.asset_id)
            except (KeyError, PermissionError, OSError):
                raise ValueError("invalid_request") from None
            if info != part.asset:
                raise ValueError("invalid_request")
            limit = media_limits.get(kind)
            if (
                limit is None
                or info.size > limit["max_bytes"]
                or info.media_type not in limit["formats"]
            ):
                raise ValueError("media_limit")
            if kind == "image":
                image_count += 1
                image_bytes += info.size
                if image_count > max_images_per_message or image_bytes > limit["max_bytes"]:
                    raise ValueError("media_limit")
            try:
                content = await ctx.assets.read(info.asset_id)
            except (KeyError, PermissionError, OSError):
                raise ValueError("invalid_request") from None
            if len(content) > limit["max_bytes"]:
                raise ValueError("media_limit")
            if len(content) != info.size or hashlib.sha256(content).hexdigest() != info.asset_id:
                raise ValueError("invalid_request")
            kind = "record" if kind == "audio" else kind
            data = {"file": "base64://" + base64.b64encode(content).decode("ascii")}
        else:
            raise ValueError("invalid_request")
        segments.append({"type": kind, "data": data})
    if not segments:
        raise ValueError("invalid_request")
    return segments
