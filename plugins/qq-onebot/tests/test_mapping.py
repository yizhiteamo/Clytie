import importlib.util
import json
from pathlib import Path

import pytest

from clytie.sdk.testing import FakeContext, FakeIdentity
from clytie.sdk.types import (
    Actor,
    AudioPart,
    CardPart,
    DeliveryIntent,
    FilePart,
    ImagePart,
    LinkPart,
    MentionPart,
    MessageRef,
    QuotePart,
    StickerPart,
    TextPart,
    UnknownPart,
    VideoPart,
)


@pytest.fixture
def mapping():
    path = Path(__file__).parents[1] / "mapping.py"
    assert path.exists(), "message mapping not implemented"
    spec = importlib.util.spec_from_file_location("qq_mapping_tests", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ctx(tmp_path):
    return FakeContext(
        root=tmp_path,
        channel=True,
        permissions=["assets.write"],
        identity=FakeIdentity("qq_test", self_account="1000000000"),
    )


def segment(kind, **data):
    return {"type": kind, "data": data}


def event(parts):
    return dict(
        post_type="message",
        message_type="group",
        self_id=1000000000,
        user_id=1000000001,
        group_id=1000000003,
        message_id=-42,
        time=1_700_000_000,
        message=parts,
    )


def intent(parts):
    return DeliveryIntent(
        intent_id="a" * 32,
        trace_id="b" * 32,
        companion_id="companion_a",
        conversation_id="test_group",
        channel_id="qq_test",
        kind="reply",
        parts=parts,
    )


def test_qq_mapping_all_inbound_parts(mapping, ctx):
    parts = [
        segment("text", text="hello"),
        segment("at", qq="1000000000"),
        segment("image", url="https://example.invalid/a"),
        segment("record", file="audio"),
        segment("video", file="video"),
        segment("file", name="fictional"),
        segment("reply", id="-41"),
        segment("face", id=12),
        segment("mface", summary="x" * 120),
        segment("future", x=1),
    ]
    result, malformed = mapping.normalize_event(ctx, event(parts), "1000000000")
    assert malformed == 0
    assert result.event_id == "qq_test:-42"
    assert result.message.sender == ctx.identity.resolve_actor("1000000001")
    actual = result.message.parts
    assert [p.type for p in actual] == [
        "text",
        "mention",
        "image",
        "audio",
        "video",
        "file",
        "quote",
        "sticker",
        "sticker",
        "unknown",
    ]
    assert actual[1].is_self
    assert actual[2].asset is None and actual[2].raw_ref == "https://example.invalid/a"
    assert actual[5].raw_ref == json.dumps(
        {"name": "fictional"}, sort_keys=True, ensure_ascii=False
    )
    assert actual[6].message_ref.platform_message_id == "-41"
    assert actual[7].sticker_id == "face:12"
    assert actual[8].summary == "x" * 100
    assert actual[9].raw_type == "future"
    again, _ = mapping.normalize_event(ctx, event(parts), "1000000000")
    assert again.message.parts[8].sticker_id == actual[8].sticker_id


def test_qq_mapping_uses_first_valid_reply_as_target(mapping, ctx):
    segments = [segment("reply", id=None), segment("reply", id="-41"), segment("reply", id="7")]
    result, malformed = mapping.normalize_event(ctx, event(segments), "1000000000")
    assert malformed == 1
    unknown, first, second = result.message.parts
    assert isinstance(unknown, UnknownPart)
    assert isinstance(first, QuotePart) and isinstance(second, QuotePart)
    assert [p.message_ref.platform_message_id for p in (first, second)] == ["-41", "7"]
    assert result.message.reply_to == first.message_ref
    result, _ = mapping.normalize_event(ctx, event(segments[:1]), "1000000000")
    assert result.message.reply_to is None


@pytest.mark.parametrize(
    "changes",
    [
        {"self_id": 1000000002},
        {"user_id": 1000000000},
        {"message_type": "notice"},
        {"message": "CQ"},
        {"time": True},
        {"message_id": None},
        {"group_id": []},
        {"user_id": True},
        {"post_type": "notice"},
        {"message_type": []},
    ],
)
def test_qq_mapping_drops_invalid_roots(mapping, ctx, changes):
    payload = event([])
    payload.update(changes)
    assert mapping.normalize_event(ctx, payload, "1000000000")[0] is None


def test_qq_mapping_private_identity(mapping, ctx):
    payload = event([segment("at", qq="1000000001")])
    payload["message_type"] = "private"
    result, malformed = mapping.normalize_event(ctx, payload, "1000000000")
    assert malformed == 0
    assert result.conversation == ctx.identity.resolve_conversation("private", "1000000001")
    assert not result.message.parts[0].is_self


@pytest.mark.parametrize("kind", ["file", "mface"])
@pytest.mark.parametrize("data,bad", [({}, 0), (None, 1), ([], 1), ({"x": "字" * 1400}, 1)])
def test_qq_mapping_extension_data(mapping, ctx, kind, data, bad):
    result, count = mapping.normalize_event(
        ctx, event([{"type": kind, "data": data}]), "1000000000"
    )
    part = result.message.parts[0]
    assert count == bad
    assert (part.raw_ref is None) == bool(bad)
    if kind == "mface" and bad:
        assert part.sticker_id == "mface:unknown"


@pytest.mark.parametrize(
    "part",
    [
        None,
        {},
        segment("face"),
        segment("face", id=True),
        segment("face", id="oops"),
        segment("text", text=1),
        segment("at", qq=[]),
        segment("reply", id=None),
        segment("image"),
    ],
)
def test_qq_mapping_malformed_segments(mapping, ctx, part):
    result, count = mapping.normalize_event(ctx, event([part]), "1000000000")
    assert count == 1
    assert result.message.parts[0].type == "unknown"


async def test_qq_mapping_encode_basic_parts(mapping, ctx):
    ref = MessageRef(channel_id="qq_test", conversation_id="test_group", platform_message_id="-41")
    parts = [
        TextPart(text="hello"),
        MentionPart(actor_id="self", is_self=True),
        QuotePart(message_ref=ref),
        StickerPart(sticker_id="face:12"),
    ]
    assert await mapping.encode_parts(ctx, intent(parts), {}) == [
        segment("text", text="hello"),
        segment("at", qq="1000000000"),
        segment("reply", id="-41"),
        segment("face", id="12"),
    ]


@pytest.mark.parametrize(
    "part",
    [
        FilePart(),
        StickerPart(sticker_id="mface:any"),
        UnknownPart(raw_type="future"),
        CardPart(title="a", body="b"),
        LinkPart(url="https://example.invalid"),
        MentionPart(actor_id="missing", is_self=False),
        ImagePart(raw_ref="file:///local"),
    ],
)
async def test_qq_mapping_rejects_unsupported(mapping, ctx, part):
    with pytest.raises(ValueError, match="^invalid_request$"):
        await mapping.encode_parts(ctx, intent([TextPart(text="first"), part]), {})


@pytest.mark.parametrize(
    "cls,kind,wire,mime",
    [
        (ImagePart, "image", "image", "image/png"),
        (AudioPart, "audio", "record", "audio/wav"),
        (VideoPart, "video", "video", "video/mp4"),
    ],
)
async def test_qq_mapping_media_authorized_bytes(mapping, ctx, cls, kind, wire, mime):
    asset = await ctx.asset_writer.put(b"abc", mime, "test")
    limits = {kind: {"max_bytes": 3, "formats": [mime]}}
    assert await mapping.encode_parts(ctx, intent([cls(asset=asset)]), limits) == [
        segment(wire, file="base64://YWJj")
    ]
    for invalid in [
        {},
        {kind: {"max_bytes": 2, "formats": [mime]}},
        {kind: {"max_bytes": 3, "formats": []}},
    ]:
        with pytest.raises(ValueError, match="^media_limit$"):
            await mapping.encode_parts(ctx, intent([cls(asset=asset)]), invalid)


async def test_qq_mapping_rejects_asset_metadata_mismatch(mapping, ctx):
    asset = await ctx.asset_writer.put(b"abc", "image/png", "test")
    forged = asset.model_copy(update={"size": 1})
    with pytest.raises(ValueError, match="^invalid_request$"):
        await mapping.encode_parts(
            ctx,
            intent([ImagePart(asset=forged)]),
            {"image": {"max_bytes": 3, "formats": ["image/png"]}},
        )


async def test_qq_mapping_rejects_asset_access_and_changed_bytes(mapping, ctx):
    asset = await ctx.asset_writer.put(b"abc", "image/png", "test")
    limits = {"image": {"max_bytes": 3, "formats": ["image/png"]}}
    ctx.assets._items[asset.asset_id] = (asset, b"def")
    with pytest.raises(ValueError, match="^invalid_request$"):
        await mapping.encode_parts(ctx, intent([ImagePart(asset=asset)]), limits)
    del ctx.assets._items[asset.asset_id]
    with pytest.raises(ValueError, match="^invalid_request$"):
        await mapping.encode_parts(ctx, intent([ImagePart(asset=asset)]), limits)


@pytest.mark.parametrize("kind", ["file", "mface"])
def test_qq_mapping_missing_extension_data(mapping, ctx, kind):
    result, count = mapping.normalize_event(ctx, event([{"type": kind}]), "1000000000")
    assert count == 1
    assert result.message.parts[0].raw_ref is None


async def test_qq_mapping_cross_conversation_quote_rejected(mapping, ctx):
    ref = MessageRef(channel_id="qq_test", conversation_id="other", platform_message_id="1")
    with pytest.raises(ValueError, match="^invalid_request$"):
        await mapping.encode_parts(ctx, intent([QuotePart(message_ref=ref)]), {})


async def test_qq_mapping_reverse_actor_identity(mapping, ctx):
    ctx.identity.actors["1000000001"] = Actor(actor_id="owner", role="owner", channel_id="qq_test")
    assert await mapping.encode_parts(
        ctx, intent([MentionPart(actor_id="owner", is_self=False)]), {}
    ) == [segment("at", qq="1000000001")]


@pytest.mark.parametrize("identifier", [0, "12", "0012"])
def test_qq_mapping_face_numeric_ids(mapping, ctx, identifier):
    result, count = mapping.normalize_event(
        ctx, event([segment("face", id=identifier)]), "1000000000"
    )
    assert count == 0
    assert result.message.parts[0].sticker_id == f"face:{identifier}"


def test_qq_mapping_mface_raw_ref_sorted_utf8(mapping, ctx):
    result, count = mapping.normalize_event(
        ctx, event([segment("mface", z="字", a=1), segment("mface", a=1, z="字")]), "1000000000"
    )
    assert count == 0
    first, second = result.message.parts
    assert first.raw_ref == '{"a": 1, "z": "字"}'
    assert first.sticker_id == second.sticker_id


async def test_qq_mapping_all_mention_inbound_only(mapping, ctx):
    result, count = mapping.normalize_event(ctx, event([segment("at", qq="all")]), "1000000000")
    assert count == 0
    part = result.message.parts[0]
    assert part.type == "mention"
    assert part.actor_id == ctx.identity.resolve_actor("all").actor_id
    assert not part.is_self
    with pytest.raises(ValueError, match="^invalid_request$"):
        await mapping.encode_parts(ctx, intent([part]), {})
