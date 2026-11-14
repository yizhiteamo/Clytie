"""Asset declarations use fictional values and offline schemas only."""

import json

import pytest

from clytie.config import _Reader
from clytie.config.assets import resolved_dependencies, validate_asset_schema

A = "a" * 64
B = "b" * 64
CREDENTIAL_MARKER = "x-clytie-secret"
ASSET = {"type": "string", "x-clytie-asset": True}


def test_nested_array_optional_and_plain_text():
    schema = {"properties": {"files": {"items": ASSET}, "optional": ASSET}}
    assert resolved_dependencies(schema, {"files": [A, B], "plain": A}) == [
        {"pointer": "/files/0", "asset_id": A},
        {"pointer": "/files/1", "asset_id": B},
    ]


@pytest.mark.parametrize("keyword", ["allOf", "anyOf", "oneOf"])
def test_applicable_branches(keyword):
    branches = [{"properties": {"file": ASSET}}]
    if keyword != "oneOf":
        branches.append({"type": "object"})
    schema = {keyword: branches}
    assert resolved_dependencies(schema, {"file": A}) == [{"pointer": "/file", "asset_id": A}]


def test_reference_and_condition():
    schema = {
        "$defs": {"asset": ASSET},
        "if": {"required": ["file"]},
        "then": {"properties": {"file": {"$ref": "#/$defs/asset"}}},
        "else": {"properties": {"fallback": ASSET}},
    }
    assert resolved_dependencies(schema, {"file": A, "fallback": B}) == [
        {"pointer": "/file", "asset_id": A}
    ]


@pytest.mark.parametrize("value", [None, "", "../file", "https://example.invalid", {}, 12])
def test_invalid_asset_value(value):
    with pytest.raises(ValueError):
        resolved_dependencies(ASSET, value)


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "array", "x-clytie-asset": True},
        {"type": "string", "x-clytie-asset": "true"},
        {"custom": ASSET},
        {"not": ASSET},
        {CREDENTIAL_MARKER: True, "properties": {"file": ASSET}},
    ],
)
def test_manifest_rejects_invalid_declaration(schema, tmp_path):
    reader = _Reader(None)
    assert not reader.plugin_schema(schema, tmp_path / "plugin.json", ("config",))
    with pytest.raises(ValueError):
        validate_asset_schema(schema)


@pytest.mark.parametrize("keyword", ["allOf", "anyOf"])
@pytest.mark.parametrize("marker", ["x-clytie-secret", "x-clytie-sensitive"])
def test_applicable_branch_credential_conflict(keyword, marker, tmp_path):
    schema = {keyword: [{"properties": {"file": ASSET}}, {marker: True}]}
    with pytest.raises(ValueError):
        resolved_dependencies(schema, {"file": A})
    reader = _Reader(None)
    reader.validate({"file": A}, schema, tmp_path / "config.yaml")
    assert reader.errors


def test_unique_oneof_required():
    with pytest.raises(ValueError):
        resolved_dependencies({"oneOf": [ASSET, {"type": "string"}]}, A)


def test_real_load_home_uses_final_role_override_and_reports_origin(tmp_path):
    from tests.test_config import make_home, update_plugin, write_yaml

    from clytie.config import load_home

    home, repository, instance, companion = make_home(tmp_path)
    schema = {"properties": {"file": {**ASSET, "default": B}}}
    update_plugin(repository, schema)
    instance["plugins"]["fake-model"] = {"config": {"file": "overridden-invalid"}}
    companion["model"]["config"] = {"file": A}
    write_yaml(home / "clytie.local.yaml", instance)
    path = home / "companions/companion_a/companion.yaml"
    write_yaml(path, companion)
    report = load_home(home, repo_root=repository)
    assert report.valid, report.to_dict()
    assert resolved_dependencies(schema, report.plugin_configs["companion_a"]["model"]) == [
        {"pointer": "/file", "asset_id": A}
    ]
    from clytie.config.assets import companion_dependencies
    from clytie.plugin_host.manifests import discover

    for manifest_path in (repository / "plugins").glob("*/plugin.json"):
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        data["requires"]["sdk"] = ">=0.9,<1"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
    manifests = discover(repository / "plugins", repo_root=repository)
    assert companion_dependencies(report, "companion_a", manifests) == [
        {"pointer": "/model/config/file", "asset_id": A, "plugin_id": "fake-model", "unit": "model"}
    ]
    companion["model"]["config"]["file"] = "invalid-final"
    write_yaml(path, companion)
    report = load_home(home, repo_root=repository)
    assert any(
        error.file == str(path)
        and error.pointer == "/model/config/file"
        and error.code == "asset_id_invalid"
        for error in report.errors
    )


def test_manifest_entry_point(tmp_path):
    from tests.test_config import make_home, update_plugin

    from clytie.plugin_host.manifests import ManifestError, read_manifest

    _, repository, _, _ = make_home(tmp_path)
    path = update_plugin(repository, {"properties": {"file": {**ASSET, CREDENTIAL_MARKER: True}}})
    with pytest.raises(ManifestError, match="asset_credential_conflict"):
        read_manifest(path, repo_root=repository)


def test_branch_selection_with_local_reference():
    schema = {
        "$defs": {"a": {"const": A}},
        "anyOf": [{"$ref": "#/$defs/a", **ASSET}, {"type": "string"}],
    }
    assert resolved_dependencies(schema, A) == [{"pointer": "", "asset_id": A}]
    assert resolved_dependencies(schema, B) == []


def test_inactive_branch_secret_is_not_a_conflict():
    schema = {"anyOf": [ASSET, {"const": B, CREDENTIAL_MARKER: True}]}
    assert resolved_dependencies(schema, A) == [{"pointer": "", "asset_id": A}]


def test_ref_cannot_hide_secret_ancestor():
    schema = {
        "$defs": {"asset": ASSET},
        "x-clytie-sensitive": True,
        "properties": {"file": {"$ref": "#/$defs/asset"}},
    }
    with pytest.raises(ValueError, match="credential_conflict"):
        resolved_dependencies(schema, {"file": A})


@pytest.mark.parametrize(
    "reference", ["https://example.invalid/schema", "file:///other.json", "#/absent"]
)
def test_references_remain_offline_and_resolvable(reference):
    with pytest.raises(ValueError):
        validate_asset_schema({"properties": {"file": ASSET}, "$ref": reference})


def test_same_asset_keeps_field_sources_and_escapes_pointer():
    schema = {"properties": {"a/b": ASSET, "a~b": ASSET}}
    assert resolved_dependencies(schema, {"a/b": A, "a~b": A}) == [
        {"pointer": "/a~1b", "asset_id": A},
        {"pointer": "/a~0b", "asset_id": A},
    ]


@pytest.mark.parametrize(
    "node",
    [
        {"not": {"$ref": "#/$defs/a"}},
        {"unevaluatedProperties": {CREDENTIAL_MARKER: True}},
    ],
)
def test_unsupported_applicators_cannot_hide_declarations(node):
    schema = {"$defs": {"a": ASSET}, **node}
    with pytest.raises(ValueError, match="unsupported_location"):
        validate_asset_schema(schema)


def test_supported_array_and_object_applicators():
    schema = {
        "properties": {"list": {"prefixItems": [ASSET], "items": ASSET}},
        "patternProperties": {"^extra": ASSET},
        "additionalProperties": ASSET,
    }
    dependencies = resolved_dependencies(schema, {"list": [A, B], "extra1": A, "other": B})
    assert {entry["pointer"] for entry in dependencies} == {
        "/list/0",
        "/list/1",
        "/extra1",
        "/other",
    }
