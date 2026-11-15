import pytest
from pydantic import ValidationError

from clytie.sdk.types import StickerPart


def test_old_sticker_parses_without_new_fields():
    sticker = StickerPart.model_validate({"type": "sticker", "sticker_id": "face:1"})
    assert sticker.raw_ref is None and sticker.summary is None


def test_sticker_raw_reference_and_summary_roundtrip():
    sticker = StickerPart(
        sticker_id="mface:fictional", raw_ref='{"summary":"a"}', summary="a" * 100
    )
    assert StickerPart.model_validate_json(sticker.model_dump_json()) == sticker
    with pytest.raises(ValidationError):
        StickerPart(sticker_id="mface:fictional", summary="a" * 101)
