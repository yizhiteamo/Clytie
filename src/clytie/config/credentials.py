"""离线凭据声明解析；无法稳定证明字段位置时拒绝捕获。"""

from jsonschema import SchemaError

from .assets import _Schema

_ANNOTATIONS = {"x-clytie-secret", "x-clytie-sensitive"}
_LOCATIONS = {
    "properties",
    "items",
    "prefixItems",
    "allOf",
    "$defs",
    "definitions",
}
_MISSING = object()


def credential_pointers(schema, value):
    """Return unconditional declaration paths, including absent required fields.

    Use the existing offline fragment resolver. Branch predicates depending on
    removed credentials cannot be reconstructed during restore; those schemas
    are opaque instead of guessing a branch or substituting credential values.
    """
    try:
        document = _Schema(schema)
        document.check()

        def has_secret(node, active=frozenset()):
            if id(node) in active:
                return False
            active = active | {id(node)}
            if isinstance(node, dict):
                # False opts out. Non-boolean annotations still fail closed,
                # including when hidden in an otherwise unsupported branch.
                if any(node[key] is not False for key in _ANNOTATIONS & node.keys()):
                    return True
                if "$ref" in node and has_secret(document.resolve(node["$ref"]), active):
                    return True
                return any(has_secret(child, active) for child in node.values())
            return isinstance(node, list) and any(has_secret(child, active) for child in node)

        # Inspect schema nodes, not the property-name maps containing them.
        def inspect_schema(node):
            if isinstance(node, dict):
                for key, child in node.items():
                    if key in _ANNOTATIONS and type(child) is not bool:
                        raise ValueError("opaque_credential_config")
                    if key not in _LOCATIONS and key not in _ANNOTATIONS and has_secret(child):
                        raise ValueError("opaque_credential_config")
                    if key in {"properties", "$defs", "definitions"}:
                        for nested in child.values():
                            inspect_schema(nested)
                    elif key in {"allOf", "prefixItems"}:
                        for nested in child:
                            inspect_schema(nested)
                    elif key == "items":
                        inspect_schema(child)

        inspect_schema(document.root)
        paths = set()

        def walk(node, item, parts=(), active=frozenset()):
            if not isinstance(node, dict):
                return
            if id(node) in active:
                raise ValueError("opaque_credential_config")
            active = active | {id(node)}
            if any(node.get(key) is True for key in _ANNOTATIONS):
                paths.add(parts)
                return
            if "$ref" in node:
                walk(document.resolve(node["$ref"]), item, parts, active)
            for branch in node.get("allOf", []):
                walk(branch, item, parts, active)
            for name, child in node.get("properties", {}).items():
                selected = item.get(name, _MISSING) if isinstance(item, dict) else _MISSING
                walk(child, selected, (*parts, name), active)
            if isinstance(item, list):
                prefix = node.get("prefixItems", [])
                for index, child in enumerate(item):
                    branch = prefix[index] if index < len(prefix) else node.get("items")
                    walk(branch, child, (*parts, index), active)

        walk(document.root, value)
        # An ancestor exclusion already covers descendants; avoid duplicate proof
        # rows and avoid navigating into an already removed subtree on redaction.
        return [
            parts
            for parts in sorted(paths, key=repr)
            if not any(parts[: len(parent)] == parent for parent in paths if parent != parts)
        ]
    except (SchemaError, ValueError, TypeError, KeyError, RecursionError) as exc:
        raise ValueError("opaque_credential_config") from exc
