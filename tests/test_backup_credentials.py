"""恢复排除的精确证明与普通配置校验必须分开。"""

import json
import shutil
from pathlib import Path

import pytest
import yaml

from clytie.backup.credentials import credential_exclusions, redact_document, validate_restored
from clytie.config import load_home

ROOT = Path(__file__).resolve().parents[1]


def fictional(tmp_path):
    home = tmp_path / "home"
    shutil.copytree(ROOT / "examples/home", home)
    file = home / "clytie.local.yaml"
    config = yaml.safe_load(file.read_text(encoding="utf-8"))
    config["accounts"]["example_bot"]["secret"] = "fictional.json"
    file.write_text(yaml.safe_dump(config), encoding="utf-8")
    return home


def test_missing_reference_is_data_ready_not_startup_valid(tmp_path):
    home = fictional(tmp_path)
    exclusions = credential_exclusions(home, repo_root=ROOT)
    result = validate_restored(home, exclusions, repo_root=ROOT)
    assert result["data_valid"]
    assert not result["configuration"]["valid"]
    assert result["credential_exclusions"][0]["pointer"] == "/accounts/example_bot/secret"
    assert load_home(home, repo_root=ROOT).global_errors


@pytest.mark.parametrize("change", ["undeclared", "wrong_schema", "wrong_pointer", "escape"])
def test_exclusion_does_not_hide_other_errors(tmp_path, change):
    home = fictional(tmp_path)
    exclusions = credential_exclusions(home, repo_root=ROOT)
    if change == "undeclared":
        exclusions = []
    elif change == "wrong_schema":
        exclusions[0]["schema_sha256"] = "a" * 64
    elif change == "wrong_pointer":
        exclusions[0]["pointer"] = "/people"
    else:
        file = home / "clytie.local.yaml"
        file.write_text(file.read_text().replace("fictional.json", "../outside.json"))
    assert not validate_restored(home, exclusions, repo_root=ROOT)["data_valid"]


def test_other_global_error_is_never_credential_exclusion(tmp_path):
    home = fictional(tmp_path)
    exclusions = credential_exclusions(home, repo_root=ROOT)
    file = home / "clytie.local.yaml"
    file.write_text(file.read_text() + "unexpected: harmless\n")
    result = validate_restored(home, exclusions, repo_root=ROOT)
    assert not result["data_valid"]
    assert any(e["pointer"] == "/unexpected" for e in result["configuration"]["global_errors"])


def test_status_rechecks_after_credentials_are_supplied(tmp_path):
    home = fictional(tmp_path)
    exclusions = credential_exclusions(home, repo_root=ROOT)
    (home / "secrets").mkdir()
    (home / "secrets/fictional.json").write_text("harmless")
    assert validate_restored(home, exclusions, repo_root=ROOT)["configuration"]["valid"]
    file = home / "clytie.local.yaml"
    file.write_text(file.read_text() + "unexpected: harmless\n")
    assert not validate_restored(home, exclusions, repo_root=ROOT)["configuration"]["valid"]


def test_declared_inline_required_credential_and_other_schema_errors(tmp_path):
    home = fictional(tmp_path)
    manifest = json.loads((ROOT / "plugins/fake-model/plugin.json").read_text(encoding="utf-8"))
    manifest["id"] = "fictional-model"
    manifest["config"] = {
        "type": "object",
        "required": ["credential"],
        "properties": {
            "credential": {
                "type": "string",
                "x-clytie-secret": True,
            },
            "ordinary": {"type": "integer"},
        },
        "additionalProperties": False,
    }
    plugin = home / "plugins/fictional-model"
    plugin.mkdir(parents=True)
    (plugin / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    file = home / "companions/companion_a/companion.yaml"
    config = yaml.safe_load(file.read_text(encoding="utf-8"))
    config["model"] = {"plugin": "fictional-model", "config": {}}
    file.write_text(yaml.safe_dump(config), encoding="utf-8")
    exclusions = credential_exclusions(home, repo_root=ROOT)
    result = validate_restored(home, exclusions, repo_root=ROOT)
    assert result["data_valid"], result
    assert any(e["kind"] == "inline" for e in result["credential_exclusions"])
    config["model"]["config"]["ordinary"] = "not_an_integer"
    file.write_text(yaml.safe_dump(config), encoding="utf-8")
    assert not validate_restored(home, exclusions, repo_root=ROOT)["data_valid"]


def test_malformed_authenticated_declaration_is_rejected_without_crashing(tmp_path):
    home = fictional(tmp_path)
    proof = credential_exclusions(home, repo_root=ROOT)
    proof[0]["file"] = ["clytie.local.yaml"]
    assert not validate_restored(home, proof, repo_root=ROOT)["data_valid"]


def test_redaction_preserves_live_config_and_reference_without_fake_value():
    account = {
        "secret": "fake-secret",
    }
    config = {"account": account, "config": {"credential": "harmless", "n": 3}}
    declarations = [
        dict(file="config.yaml", pointer="/config/credential", kind="inline"),
        dict(file="config.yaml", pointer="/account/secret", kind="file_reference"),
    ]
    redacted = redact_document(config, "config.yaml", declarations)
    assert redacted == {"account": account, "config": {"n": 3}}
    assert config["config"]["credential"] == "harmless"
