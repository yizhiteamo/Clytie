"""组合声明的凭据必须经过真实捕获、加密和恢复仍保持排除。"""

import json
import shutil

import pytest
import yaml

from clytie.backup.container import pack
from clytie.backup.credentials import (
    credential_exclusions,
    declared,
    document_exclusions,
    validate_restored,
)
from clytie.backup.repository import SourceRepository
from clytie.backup.restore import RestoreManager
from clytie.backup.source import capture_instance
from clytie.config import load_home

from .test_backup_source import ROOT, fictional

CREDENTIAL_ANNOTATION = "x-clytie-secret"


FIELD_SCHEMA = {"type": "string", CREDENTIAL_ANNOTATION: True}
OBJECT = {
    "type": "object",
    "properties": {"credential": FIELD_SCHEMA, "ordinary": {"type": "integer"}},
    "required": ["credential", "ordinary"],
    "additionalProperties": False,
}


def schema_for(style):
    if style == "allOf":
        return {"type": "object", "allOf": [OBJECT, {"allOf": [OBJECT]}]}
    if style == "reference":
        return {"$defs": {"account": OBJECT}, "allOf": [{"$ref": "#/$defs/account"}]}
    if style == "anchor":
        return {
            "$defs": {"account": {**OBJECT, "$anchor": "account"}},
            "allOf": [{"$ref": "#account"}],
        }
    return {
        "type": "object",
        "properties": {"accounts": {"type": "array", "items": {"allOf": [OBJECT]}}},
        "required": ["accounts"],
    }


def configured(tmp_path, style="allOf"):
    home, clock = fictional(tmp_path)
    plugin = home / "plugins/fictional-model"
    plugin.mkdir(parents=True)
    for name in ("plugin.json", "plugin.py"):
        shutil.copy2(ROOT / "plugins/fake-model" / name, plugin / name)
    path = plugin / "plugin.json"
    manifest = json.loads(path.read_bytes())
    manifest.update(id="fictional-model", config=schema_for(style))
    path.write_text(json.dumps(manifest), encoding="utf-8")
    file = home / "companions/companion_a/companion.yaml"
    document = yaml.safe_load(file.read_bytes())
    value = {"credential": "fictional-credential-do-not-archive", "ordinary": 7}
    document["model"] = {
        "plugin": "fictional-model",
        "config": {"accounts": [value]} if style == "array" else value,
    }
    file.write_text(yaml.safe_dump(document), encoding="utf-8")
    versions_file = file.parent / "plugin-versions.json"
    versions = json.loads(versions_file.read_bytes())
    versions["fictional-model"] = versions["fake-model"]
    versions_file.write_text(json.dumps(versions), encoding="utf-8")
    return home, clock, file


@pytest.mark.parametrize("style", ["allOf", "reference", "anchor", "array"])
async def test_composed_secret_excluded_through_real_package_restore(tmp_path, style):
    home, clock, file = configured(tmp_path, style)
    assert load_home(home, repo_root=ROOT).valid
    stage = tmp_path / "stage"
    captured = await capture_instance(
        home, clock, stage, scope="instance", epoch="test", source_instance_id="fictional"
    )
    assert captured["manifest"]["completeness"] == "complete", captured["missing"]
    relative = file.relative_to(home)
    assert b"fictional-credential-do-not-archive" not in (stage / relative).read_bytes()
    assert b"fictional-credential-do-not-archive" in file.read_bytes()
    exclusions = captured["manifest"]["credential_exclusions"]
    assert len([row for row in exclusions if row["kind"] == "inline"]) == 1
    package = tmp_path / "packages/source.clytie-backup"
    package.parent.mkdir()
    key = tmp_path / "fictional.key"
    key.write_bytes(b"k" * 32)
    pack(stage, package, key.read_bytes(), captured["manifest"])
    repository = SourceRepository.initialize(package.parent, clock, owner_instance_id="fictional")
    repository.register(
        dict(backup_id="fictional", path=str(package), identity=repository.identity(package))
    )
    manager = RestoreManager(tmp_path / "management", clock, reserve_bytes=0)
    manager.previews.bind_source(
        package,
        dict(
            root=str(package.parent), repository_id=repository.repository_id, backup_id="fictional"
        ),
        confirmed=True,
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = tmp_path / "target"
    result = manager.apply(manager.plan(package, key, target, workspace)["id"])
    assert result["data_state"] == "ready_for_switch"
    assert b"fictional-credential-do-not-archive" not in (target / relative).read_bytes()
    checked = validate_restored(target, exclusions, repo_root=ROOT)
    assert checked["data_valid"] and checked["credential_exclusions"]
    assert not load_home(target, repo_root=ROOT).valid
    for field, value in (("pointer", "/model/config/ordinary"), ("schema_sha256", "b" * 64)):
        tampered = [{**row, field: value} if row["kind"] == "inline" else row for row in exclusions]
        assert not validate_restored(target, tampered, repo_root=ROOT)["data_valid"]
    document = yaml.safe_load((target / relative).read_bytes())
    value = document["model"]["config"]
    if style == "array":
        value = value["accounts"][0]
    value["ordinary"] = "invalid-integer"
    (target / relative).write_text(yaml.safe_dump(document), encoding="utf-8")
    assert not validate_restored(target, exclusions, repo_root=ROOT)["data_valid"]


@pytest.mark.parametrize(
    "schema",
    [
        {"anyOf": [OBJECT, {"type": "object"}]},
        {"oneOf": [OBJECT, {"type": "string"}]},
        {"if": {"type": "object"}, "then": OBJECT},
        {"not": {"properties": {"credential": FIELD_SCHEMA}}},
        {"x-unknown": {"properties": {"credential": FIELD_SCHEMA}}},
        {"$defs": {"credential_field": FIELD_SCHEMA}, "not": {"$ref": "#/$defs/credential_field"}},
        {"properties": {"credential": {"type": "string", CREDENTIAL_ANNOTATION: "yes"}}},
        {"$ref": "https://invalid.example/schema"},
        {"$ref": "#/$defs/missing"},
        {"patternProperties": {"^credential": FIELD_SCHEMA}},
        {"additionalProperties": FIELD_SCHEMA},
    ],
)
def test_uninterpretable_secret_declaration_fails_closed(schema):
    with pytest.raises(ValueError, match="opaque_credential_config"):
        list(declared(schema, {"credential": "fictional", "ordinary": 7}))


@pytest.mark.parametrize("annotation", ["x-clytie-secret", "x-clytie-sensitive"])
@pytest.mark.parametrize("location", ["anyOf", "oneOf", "then", "patternProperties"])
def test_false_credential_annotation_is_not_an_opaque_declaration(annotation, location):
    field = {"type": "string", annotation: False}
    branch = {"properties": {"credential": field}}
    if location in {"anyOf", "oneOf"}:
        schema = {location: [branch]}
    elif location == "then":
        schema = {"if": {"type": "object"}, "then": branch}
    else:
        schema = {"patternProperties": {"^credential$": field}}
    assert list(declared(schema, {"credential": "ordinary-value"})) == []


@pytest.mark.parametrize("annotation", ["x-clytie-secret", "x-clytie-sensitive"])
def test_invalid_credential_annotation_in_branch_is_still_rejected(annotation):
    schema = {"anyOf": [{"properties": {"credential": {annotation: "false"}}}]}
    with pytest.raises(ValueError, match="opaque_credential_config"):
        list(declared(schema, {"credential": "fictional"}))


def test_composed_secret_does_not_waive_missing_ordinary_field(tmp_path):
    home, _, file = configured(tmp_path)
    exclusions = credential_exclusions(home, repo_root=ROOT)
    document = yaml.safe_load(file.read_bytes())
    document["model"]["config"] = {}
    file.write_text(yaml.safe_dump(document), encoding="utf-8")
    result = validate_restored(home, exclusions, repo_root=ROOT)
    assert not result["data_valid"]
    assert any(row["pointer"].endswith("/ordinary") for row in result["configuration"]["errors"])


async def test_opaque_declaration_fails_capture_before_copying_credentials(tmp_path):
    home, clock, _ = configured(tmp_path)
    manifest_path = home / "plugins/fictional-model/plugin.json"
    manifest = json.loads(manifest_path.read_bytes())
    manifest["config"] = {"anyOf": [OBJECT, {"type": "object"}]}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert load_home(home, repo_root=ROOT).valid
    stage = tmp_path / "stage"
    with pytest.raises(ValueError, match="opaque_credential_config"):
        await capture_instance(
            home, clock, stage, scope="instance", epoch="test", source_instance_id="fictional"
        )
    assert not (stage / "companions/companion_a/companion.yaml").exists()


@pytest.mark.parametrize("change", ["unknown", "type"])
def test_history_unknown_or_invalid_plugin_field_cannot_be_archived(tmp_path, change):
    home, _, file = configured(tmp_path)
    document = yaml.safe_load(file.read_bytes())
    config = document["model"]["config"]
    config["unknown" if change == "unknown" else "ordinary"] = "fictional-old-credential"
    with pytest.raises(ValueError, match="opaque_credential_config"):
        document_exclusions(
            home,
            "companions/companion_a/config-history/companion.yaml.old",
            document,
            document_kind="companion",
            repo_root=ROOT,
        )
