"""账本投影和写入前的参数脱敏，不向插件暴露读取接口。"""

from copy import deepcopy


def redact(value, schema):
    if not isinstance(schema, dict):
        return deepcopy(value)
    if schema.get("x-clytie-sensitive") is True:
        return "[redacted]"
    result = deepcopy(value)
    for keyword in ("oneOf", "anyOf", "allOf"):
        for branch in schema.get(keyword, []):
            result = redact(result, branch)
    if isinstance(result, dict):
        for key, item in result.items():
            result[key] = redact(item, schema.get("properties", {}).get(key, {}))
    elif isinstance(result, list):
        result = [redact(item, schema.get("items", {})) for item in result]
    return result


def visible(record, *, scene, conversation_id, private_scenes=("owner_private",)):
    return (
        scene == "owner_private"
        or record["conversation_id"] == conversation_id
        or (scene in private_scenes and record["scene"] == "owner_private")
    )
