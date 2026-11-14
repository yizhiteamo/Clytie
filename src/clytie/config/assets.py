"""Offline, schema-directed asset dependencies; never opens an asset store."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

from jsonschema import Draft202012Validator
from referencing import Registry
from referencing.exceptions import NoSuchResource

_ASSET = "x-clytie-asset"
_CREDENTIALS = ("x-clytie-secret", "x-clytie-sensitive")
_MAPS = {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
_LISTS = {"allOf", "anyOf", "oneOf", "prefixItems"}
_SINGLE = {"items", "additionalProperties", "then", "else", "contains"}


class AssetDeclarationError(ValueError):
    def __init__(self, code: str, parts: tuple = ()):
        self.parts = parts
        self.code = code
        super().__init__(code)


def _json(value):
    if isinstance(value, Mapping):
        return {key: _json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json(item) for item in value]
    return value


def _has_annotation(value):
    if isinstance(value, dict):
        return _ASSET in value or any(_has_annotation(child) for child in value.values())
    return isinstance(value, list) and any(_has_annotation(child) for child in value)


def _no_remote(uri):
    raise NoSuchResource(ref=uri)


class _Schema:
    def __init__(self, schema):
        self.root = _json(schema)
        self.nodes = set()
        self.anchors = {}
        self.references = []
        self.unsupported = []

    def inspect(self, node, parts=(), credential_covered=False):
        if not isinstance(node, dict):
            return
        self.nodes.add(id(node))
        credential_covered = credential_covered or any(
            node.get(key) is True for key in _CREDENTIALS
        )
        if _ASSET in node:
            if type(node[_ASSET]) is not bool:
                raise AssetDeclarationError("asset_annotation_boolean", parts)
            if node[_ASSET]:
                if node.get("type") != "string":
                    raise AssetDeclarationError("asset_annotation_string_node", parts)
                if credential_covered:
                    raise AssetDeclarationError("asset_credential_conflict", parts)
        if "$id" in node and parts or "$dynamicRef" in node or "$dynamicAnchor" in node:
            raise AssetDeclarationError("asset_schema_scope_unsupported", parts)
        if "$anchor" in node:
            anchor = node["$anchor"]
            if anchor in self.anchors:
                raise AssetDeclarationError("asset_schema_duplicate_anchor", parts)
            self.anchors[anchor] = node
        if "$ref" in node:
            self.references.append((node["$ref"], parts))
        for key, child in node.items():
            position = (*parts, key)
            if key in _MAPS and isinstance(child, dict):
                for name, nested in child.items():
                    self.inspect(
                        nested,
                        (*position, name),
                        False if key in {"$defs", "definitions"} else credential_covered,
                    )
            elif key in _LISTS and isinstance(child, list):
                for index, nested in enumerate(child):
                    self.inspect(nested, (*position, index), credential_covered)
            elif key in _SINGLE:
                self.inspect(child, position, credential_covered)
            elif key == "if":
                if _has_annotation(child):
                    raise AssetDeclarationError("asset_annotation_in_predicate", position)
                self.inspect(child, position, credential_covered)
                self.unsupported.append((child, position))
            elif _has_annotation(child):
                raise AssetDeclarationError("asset_annotation_unsupported_location", position)
            elif isinstance(child, (dict, list)):
                self.unsupported.append((child, position))

    def resolve(self, reference, parts=()):
        if not isinstance(reference, str) or not reference.startswith("#"):
            raise AssetDeclarationError("asset_schema_local_reference", parts)
        fragment = unquote(reference[1:])
        node = self.root
        try:
            if fragment and not fragment.startswith("/"):
                node = self.anchors[fragment]
            elif fragment:
                for component in fragment[1:].split("/"):
                    key = component.replace("~1", "/").replace("~0", "~")
                    node = node[int(key)] if isinstance(node, list) else node[key]
        except (KeyError, IndexError, TypeError, ValueError):
            raise AssetDeclarationError("asset_schema_unresolved_reference", parts) from None
        if not isinstance(node, bool) and id(node) not in self.nodes:
            raise AssetDeclarationError("asset_schema_reference_not_schema", parts)
        return node

    def check(self):
        Draft202012Validator.check_schema(self.root)
        self.inspect(self.root)
        for reference, parts in self.references:
            self.resolve(reference, parts)
        for node, parts in self.unsupported:
            if self.hidden_declaration(node):
                raise AssetDeclarationError("asset_annotation_unsupported_location", parts)

    def hidden_declaration(self, node, seen=frozenset()):
        if id(node) in seen:
            return False
        seen = seen | {id(node)}
        if isinstance(node, dict):
            if _ASSET in node or any(node.get(key) is True for key in _CREDENTIALS):
                return True
            if "$ref" in node and self.hidden_declaration(self.resolve(node["$ref"]), seen):
                return True
            return any(self.hidden_declaration(child, seen) for child in node.values())
        return isinstance(node, list) and any(
            self.hidden_declaration(child, seen) for child in node
        )


def validate_asset_schema(schema: Any) -> None:
    """Reject malformed or uninterpretable declarations during manifest validation."""
    if _has_annotation(_json(schema)):
        _Schema(schema).check()


def resolved_dependencies(schema: Any, finalvalue: Any) -> list[dict[str, str]]:
    """Return pointer/asset_id pairs from the final merged configuration only.

    Shared IDs retain every field pointer. The capture layer deduplicates content
    only after binding these declarations to its trusted companion context.
    """
    if not _has_annotation(_json(schema)):
        return []
    document = _Schema(schema)
    document.check()
    value = _json(finalvalue)
    validator = Draft202012Validator(document.root, registry=Registry(retrieve=_no_remote))
    if not validator.is_valid(value):
        raise AssetDeclarationError("asset_configuration_invalid")
    assets = {}
    credentials = set()

    def valid(node, item):
        return validator.evolve(schema=node).is_valid(item)

    def walk(node, item, parts=(), active=frozenset()):
        if not isinstance(node, dict):
            return
        identity = (id(node), parts)
        if identity in active:
            raise AssetDeclarationError("asset_schema_recursive_evaluation", parts)
        active = active | {identity}
        if any(node.get(key) is True for key in _CREDENTIALS):
            credentials.add(parts)
        if node.get(_ASSET) is True:
            if not isinstance(item, str) or re.fullmatch(r"[0-9a-f]{64}", item) is None:
                raise AssetDeclarationError("asset_id_invalid", parts)
            assets[parts] = item
        if "$ref" in node:
            walk(document.resolve(node["$ref"]), item, parts, active)
        for key in ("allOf", "anyOf", "oneOf"):
            for branch in node.get(key, []):
                if key == "allOf" or valid(branch, item):
                    walk(branch, item, parts, active)
        if "if" in node:
            branch = "then" if valid(node["if"], item) else "else"
            walk(node.get(branch), item, parts, active)
        if isinstance(item, dict):
            properties = node.get("properties", {})
            patterns = node.get("patternProperties", {})
            for key, child in item.items():
                matched = False
                if key in properties:
                    walk(properties[key], child, (*parts, key), active)
                    matched = True
                for pattern, branch in patterns.items():
                    if re.search(pattern, key):
                        walk(branch, child, (*parts, key), active)
                        matched = True
                if not matched:
                    walk(node.get("additionalProperties"), child, (*parts, key), active)
            for key, branch in node.get("dependentSchemas", {}).items():
                if key in item:
                    walk(branch, item, parts, active)
        if isinstance(item, list):
            prefix = node.get("prefixItems", [])
            for index, child in enumerate(item):
                branch = prefix[index] if index < len(prefix) else node.get("items")
                walk(branch, child, (*parts, index), active)
                if "contains" in node and valid(node["contains"], child):
                    walk(node["contains"], child, (*parts, index), active)

    walk(document.root, value)
    for parts in assets:
        if any(
            parts[: len(credential_covered)] == credential_covered
            for credential_covered in credentials
        ):
            raise AssetDeclarationError("asset_credential_conflict", parts)
    return [
        {
            "pointer": "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts),
            "asset_id": asset_id,
        }
        for parts, asset_id in sorted(assets.items(), key=lambda entry: repr(entry[0]))
    ]


def companion_dependencies(report, companion_id, manifests) -> list[dict[str, str]]:
    """Use load_home's effective role values, without merging configuration again.

    Pointers identify effective configuration fields within this companion, not
    an assertion that a value originated in that physical YAML file. The caller
    supplies the trusted companion ID and owns overall report validation.
    """
    for plugin_id in report.instance.get("notify", {}).get("plugins", ()):
        manifest = manifests.get(plugin_id)
        if manifest is None:
            raise AssetDeclarationError("asset_notifier_manifest_missing")
        if _has_annotation(_json(manifest.data["config"])):
            raise AssetDeclarationError("asset_notifier_ownership_undetermined")
    configurations = report.plugin_configs.get(companion_id)
    if configurations is None:
        raise AssetDeclarationError("asset_companion_configuration_missing")
    config = configurations["config"]
    result = []
    role_plugins = set()

    def collect(plugin_id, value, prefix, unit):
        manifest = manifests.get(plugin_id)
        if manifest is None:
            raise AssetDeclarationError("asset_plugin_manifest_missing")
        for dependency in resolved_dependencies(manifest.data["config"], value):
            result.append(
                {
                    **dependency,
                    "pointer": prefix + dependency["pointer"],
                    "plugin_id": plugin_id,
                    "unit": unit,
                }
            )

    for selected, key, prefix in (
        (config.get("model"), "model", "/model/config"),
        (
            config.get("memory", {}).get("recollect", {}).get("model"),
            "recollect_model",
            "/memory/recollect/model/config",
        ),
    ):
        if selected:
            plugin_id = selected["plugin"]
            role_plugins.add(plugin_id)
            collect(plugin_id, configurations[key], prefix, key)
    for index, selected in enumerate(config.get("channels", ())):
        plugin_id = selected["plugin"]
        role_plugins.add(plugin_id)
        collect(
            plugin_id,
            configurations["channels"][index],
            f"/channels/{index}/config",
            f"channel:{index}",
        )
    for plugin_id, value in configurations.get("plugins", {}).items():
        if plugin_id not in role_plugins:
            escaped = plugin_id.replace("~", "~0").replace("/", "~1")
            collect(plugin_id, value, f"/plugins/{escaped}/config", "plugins")
    return result
