"""仅解析账本已定义的资产字段，正文、工具参数和自由 data 不参与依赖推断。"""

import re

from clytie.sdk.types import AssetRef


def ledger_references(table, value, *, details=None):
    found = set()

    def ref(raw):
        value = AssetRef.model_validate(raw)
        found.add(value.asset_id)
        if details is not None:
            details.append(value)

    def parts(rows):
        for part in rows or ():
            if part.get("type") in {"image", "audio", "video", "file", "sticker"}:
                if part.get("asset") is not None:
                    ref(part["asset"])

    try:
        if table == "inbound_media":
            if value["status"] == "ok":
                # The ledger authenticates its own source metadata. A previous
                # tool product can register the same bytes under another origin.
                media = AssetRef.model_validate(value["asset"])
                if value["asset_id"] != media.asset_id:
                    raise ValueError
                found.add(media.asset_id)
            elif value.get("asset_id") is not None or value.get("asset") is not None:
                raise ValueError
        elif table == "inbound":
            message = value.get("message")
            if message is not None:
                parts(message.get("parts"))
        elif table == "intent":
            parts(value.get("parts"))
        elif table == "invocation":
            for asset_id in value.get("asset_ids", ()):
                if not isinstance(asset_id, str) or not re.fullmatch(r"[0-9a-f]{64}", asset_id):
                    raise ValueError
                found.add(asset_id)
            result = value.get("result")
            if result is not None:
                for row in result.get("assets", ()):
                    ref(row)
        elif table == "audit" and value.get("kind") == "late_result":
            result = value.get("content")
            if result is not None:
                for row in result.get("assets", ()):
                    ref(row)
        return found
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("asset_reference_invalid") from exc
