import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from tests.test_config import make_home, write_yaml

from clytie.config import load_home


def test_backup_defaults(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    report = load_home(home, repo_root=repository)
    assert report.valid
    assert report.instance["backup"]["enabled"] is True
    assert report.instance["backup"]["memory"]["debounce_seconds"] == 300
    assert report.instance["backup"]["retention"]["monthly_months"] is None
    assert report.instance["backup"]["mirror"]["path"] is None


@pytest.mark.parametrize(
    "backup,pointer",
    [
        ({"memory": {"debounce_seconds": 0}}, "/backup/memory/debounce_seconds"),
        ({"schedule": {"at": "24:00"}}, "/backup/schedule/at"),
        ({"schedule": {"days": [0]}}, "/backup/schedule/days/0"),
        ({"retention": {"minimum_per_group": 2}}, "/backup/retention/minimum_per_group"),
        ({"retention": {"minimum_per_group": 13}}, "/backup/retention/recent_memory"),
        ({"mirror": {"minimum": 8}}, "/backup/mirror/recent"),
        ({"mirror": {"enabled": True}}, "/backup/mirror/path"),
        ({"logs": {"archives": True}}, "/backup/logs/archives"),
        ({"disk": {"reserve_gib": 0}}, "/backup/disk/reserve_gib"),
        ({"unknown": 1}, "/backup/unknown"),
    ],
)
def test_invalid_backup_configuration(tmp_path, backup, pointer):
    home, repository, instance, _ = make_home(tmp_path)
    instance["backup"] = backup
    write_yaml(home / "clytie.local.yaml", instance)
    report = load_home(home, repo_root=repository)
    assert not report.valid
    assert any(issue.pointer == pointer for issue in report.global_errors)


def test_backup_layers_can_be_disabled_and_capacity_unbounded(tmp_path):
    home, repository, instance, _ = make_home(tmp_path)
    instance["backup"] = {
        "retention": {"daily_days": 0, "weekly_weeks": 0, "monthly_months": 0, "target_gib": None}
    }
    write_yaml(home / "clytie.local.yaml", instance)
    assert load_home(home, repo_root=repository).valid


@pytest.mark.parametrize(
    "mode,factory,valid",
    [
        (None, False, True),
        ("unsupported", False, True),
        ("stateless", False, True),
        ("snapshot", True, True),
        ("snapshot", False, False),
        (None, True, False),
        ("stateless", True, False),
        ("unsupported", True, False),
    ],
)
def test_backup_manifest_mode_factory_contract(tmp_path, mode, factory, valid):
    _, repository, _, _ = make_home(tmp_path)
    manifest = json.loads((repository / "plugins/fake-model/plugin.json").read_text())
    if mode is not None:
        manifest["backup"] = {"mode": mode}
    if factory:
        manifest["contributes"]["backup"] = {"snapshot": {"factory": "snapshot_provider"}}
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/plugin.schema.json").read_text()
    )
    assert Draft202012Validator(schema).is_valid(manifest) is valid


@pytest.mark.parametrize(
    "reference,code",
    [
        ("missing.key", "credential_reference_missing"),
        ("../outside.key", "credential_reference_invalid"),
    ],
)
def test_secret_diagnostics_do_not_relax_normal_validation(tmp_path, reference, code):
    home, repository, instance, _ = make_home(tmp_path)
    instance["accounts"] = {"test_account": {"channel": "fake", "account": "fictional"}}
    instance["accounts"]["test_account"]["secret"] = reference
    write_yaml(home / "clytie.local.yaml", instance)
    report = load_home(home, repo_root=repository)
    assert not report.valid
    issue = next(i for i in report.global_errors if i.pointer == "/accounts/test_account/secret")
    assert issue.code == code
    assert issue.to_dict()["code"] == code


def test_required_schema_diagnostic_has_exact_property(tmp_path):
    home, repository, instance, _ = make_home(tmp_path)
    del instance["timezone"]
    write_yaml(home / "clytie.local.yaml", instance)
    report = load_home(home, repo_root=repository)
    issue = next(i for i in report.global_errors if i.pointer == "/timezone")
    assert issue.code == "schema_required"


def test_invalid_manifest_backup_type_remains_diagnostic(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    path = repository / "plugins/fake-model/plugin.json"
    data = json.loads(path.read_text())
    data["backup"] = None
    path.write_text(json.dumps(data))
    report = load_home(home, repo_root=repository)
    assert not report.valid
    assert any(i.pointer == "/backup" for i in report.errors)
