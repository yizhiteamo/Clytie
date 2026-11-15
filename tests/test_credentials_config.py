"""Plugin credential declarations use fictional temporary homes only."""

import json
from pathlib import Path

import pytest
from tests.test_config import make_home, write_yaml

from clytie.backup.credentials import credential_exclusions, redact_document, validate_restored
from clytie.config import load_home

CREDENTIAL_FILE = "secret"


def credential_home(tmp_path, relative="nested/example.key"):
    home, repository, instance, _ = make_home(tmp_path)
    instance["credentials"] = {
        "image_api_test": {CREDENTIAL_FILE: relative, "plugins": ["fake-model"]}
    }
    write_yaml(home / "clytie.local.yaml", instance)
    return home, repository, instance


def test_credentials_validate_without_reading_secret(tmp_path, monkeypatch):
    home, repository, _ = credential_home(tmp_path)
    credential_path = home / "secrets/nested/example.key"
    credential_path.parent.mkdir(parents=True)
    credential_path.write_bytes(b"\xff fictional bytes need not be decoded at startup")
    original = Path.open

    def guarded(path, *args, **kwargs):
        assert not path.is_relative_to(home / "secrets"), "secret content was opened"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    report = load_home(home, repo_root=repository)
    assert report.valid, report.to_dict()
    assert "example.key" not in json.dumps(report.to_dict())


@pytest.mark.parametrize(
    "value",
    [
        {CREDENTIAL_FILE: "hidden.key", "plugins": []},
        {CREDENTIAL_FILE: "hidden.key", "plugins": ["unknown-plugin"]},
        {CREDENTIAL_FILE: "hidden.key", "plugins": ["fake-model"], "extra": "hidden.key"},
        {CREDENTIAL_FILE: {"path": "hidden.key"}, "plugins": ["fake-model"]},
    ],
)
def test_invalid_credential_declarations_do_not_expose_paths(tmp_path, value):
    home, repository, instance = credential_home(tmp_path)
    instance["credentials"]["image_api_test"] = value
    write_yaml(home / "clytie.local.yaml", instance)
    report = load_home(home, repo_root=repository)
    assert not report.valid and report.global_errors
    assert "hidden.key" not in json.dumps(report.to_dict())


@pytest.mark.parametrize("relative", ["../outside.key", "/outside.key", "C:\\outside.key", "."])
def test_credential_path_escape_and_directories_are_invalid(tmp_path, relative):
    home, repository, _ = credential_home(tmp_path, relative)
    (home / "secrets").mkdir()
    report = load_home(home, repo_root=repository)
    assert any(e.code == "credential_reference_invalid" for e in report.global_errors)


@pytest.mark.parametrize("component", ["file", "parent", "secrets"])
@pytest.mark.parametrize("group", ["credentials", "accounts"])
def test_credential_symlinks_are_invalid_even_when_target_is_inside(tmp_path, component, group):
    home, repository, instance = credential_home(tmp_path)
    if group == "accounts":
        instance.pop("credentials")
        instance["accounts"]["example_bot"]["secret"] = "nested/example.key"
        write_yaml(home / "clytie.local.yaml", instance)
    root = home / "secrets"
    target = home / "target" if component == "secrets" else root / "target"
    target.mkdir(parents=True)
    (target / "example.key").write_text("fictional marker", encoding="utf-8")
    try:
        if component == "secrets":
            root.symlink_to(target, target_is_directory=True)
        elif component == "parent":
            (root / "nested").symlink_to(target, target_is_directory=True)
        else:
            (root / "nested").mkdir()
            (root / "nested/example.key").symlink_to(target / "example.key")
    except OSError:
        pytest.skip("OS does not permit creating symlinks")
    report = load_home(home, repo_root=repository)
    assert any(e.code == "credential_reference_invalid" for e in report.global_errors)


@pytest.mark.parametrize("reference", ["", "../invalid", "invalid.name", "1invalid", "ref\n"])
def test_credential_reference_names_are_identifiers(tmp_path, reference):
    home, repository, instance = credential_home(tmp_path)
    instance["credentials"][reference] = instance["credentials"].pop("image_api_test")
    write_yaml(home / "clytie.local.yaml", instance)
    report = load_home(home, repo_root=repository)
    assert any(e.pointer == "/credentials" and e.code is None for e in report.global_errors)


def test_backup_credentials_missing_only_with_authenticated_file_reference(tmp_path):
    home, repository, instance = credential_home(tmp_path)
    proof = credential_exclusions(home, repo_root=repository)
    declaration = next(d for d in proof if d["pointer"] == "/credentials/image_api_test/secret")
    assert declaration["kind"] == "file_reference"
    assert redact_document(instance, "clytie.local.yaml", proof) == instance
    result = validate_restored(home, proof, repo_root=repository)
    assert result["data_valid"] and not result["configuration"]["valid"]
    assert {
        "file": "clytie.local.yaml",
        "pointer": declaration["pointer"],
        "kind": "file_reference",
    } in result["credential_exclusions"]
    assert not validate_restored(home, [], repo_root=repository)["data_valid"]
    instance["credentials"]["image_api_test"]["secret"] = "../outside.key"
    write_yaml(home / "clytie.local.yaml", instance)
    assert not validate_restored(home, proof, repo_root=repository)["data_valid"]
