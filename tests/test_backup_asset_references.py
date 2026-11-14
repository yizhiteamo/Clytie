import pytest

from clytie.backup.asset_references import ledger_references


def test_only_declared_ledger_fields_supply_asset_dependencies():
    ref = dict(asset_id="a" * 64, media_type="application/octet-stream", size=3, origin="fake")
    other = {**ref, "asset_id": "b" * 64}
    result = {"assets": [ref], "data": {"looks_like_asset": other}, "text": str(other)}
    assert ledger_references("invocation", {"result": result}) == {ref["asset_id"]}
    assert ledger_references("invocation", {"args": {"asset": other}}) == set()
    assert ledger_references("record", {"text": str(other), "data": other}) == set()
    assert ledger_references(
        "inbound",
        {
            "message": {
                "parts": [{"type": "text", "text": str(other)}, {"type": "file", "asset": ref}]
            }
        },
    ) == {ref["asset_id"]}
    assert ledger_references("intent", {"parts": [{"type": "sticker", "asset": ref}]}) == {
        ref["asset_id"]
    }


def test_retained_invocation_asset_ids_survive_content_retention():
    assert ledger_references("invocation", {"result": None, "asset_ids": ["a" * 64]}) == {"a" * 64}


def test_late_tool_result_is_structured_but_arbitrary_audit_content_is_not():
    ref = dict(asset_id="a" * 64, media_type="text/plain", size=3, origin="fake")
    value = {"kind": "late_result", "content": {"assets": [ref]}}
    assert ledger_references("audit", value) == {ref["asset_id"]}
    assert ledger_references("audit", {**value, "kind": "unrelated"}) == set()
    assert ledger_references("audit", {**value, "content": None}) == set()


@pytest.mark.parametrize(
    "value", [{"asset_ids": ["../outside"]}, {"result": {"assets": [{"asset_id": "a" * 64}]}}]
)
def test_invalid_structured_reference_is_not_silently_dropped(value):
    with pytest.raises(ValueError, match="asset_reference_invalid"):
        ledger_references("invocation", value)
