"""Configuration acceptance and error-location checks using handwritten fixtures."""

import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from clytie import config as configuration_module
from clytie.config import deep_merge, load_home, resolve_home, thaw

ROOT = Path(__file__).resolve().parents[1]


def write_yaml(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")


def make_home(tmp_path):
    """Make text-only test configuration; do not import plugin code."""
    repository = tmp_path / "repository"
    home = tmp_path / "home"
    for schema in (ROOT / "schemas").glob("*.json"):
        target = repository / "schemas" / schema.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
    example = ROOT / "examples" / "home"
    instance = yaml.safe_load((example / "clytie.local.yaml").read_text(encoding="utf-8"))
    companion = yaml.safe_load(
        (example / "companions" / "companion_a" / "companion.yaml").read_text(encoding="utf-8")
    )
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions" / "companion_a" / "companion.yaml", companion)
    persona = home / "companions" / "companion_a" / "persona" / "base.md"
    persona.parent.mkdir(parents=True)
    persona.write_text("Fictional test companion; no recorded experiences.", encoding="utf-8")
    for plugin_id, category in (("fake-model", "model"), ("fake-channel", "channel")):
        manifest = {
            "schema_version": 1,
            "id": plugin_id,
            "name": plugin_id,
            "summary": "Fictional offline test implementation",
            "category": category,
            "version": "0.1.0",
            "requires": {"sdk": ">=0.1,<0.2"},
            "entry": "plugin:Plugin",
            "contributes": {},
            "permissions": [],
            "config": {"type": "object", "properties": {}, "additionalProperties": False},
        }
        path = repository / "plugins" / plugin_id / "plugin.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(manifest), encoding="utf-8")
    return home, repository, instance, companion


def update_plugin(repository, schema):
    path = repository / "plugins" / "fake-model" / "plugin.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["config"] = schema
    path.write_text(json.dumps(manifest), encoding="utf-8")
    return path


def test_three_schemas_are_draft_2020_12():
    for name in ("instance", "companion", "plugin"):
        schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)


def test_valid_home_and_all_returned_config_is_read_only(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    report = load_home(home, repo_root=repository)
    assert report.valid, report.to_dict()
    assert report.exit_code == 0
    assert report.companions[0]["model"]["status"] == "found"
    assert report.plugin_configs["companion_a"]["config"]["forbidden_names"] == ("companion_a",)
    with pytest.raises(TypeError):
        report.instance["timezone"] = "UTC"
    with pytest.raises(TypeError):
        report.companions[0]["model"]["status"] = "missing"
    with pytest.raises(TypeError):
        report.plugin_configs["companion_a"]["config"]["names"]["internal"] = "changed"
    detached = report.to_dict()
    detached["companions"][0]["model"]["status"] = "changed"
    assert report.companions[0]["model"]["status"] == "found"


def test_deep_merge_replaces_lists_scalars_and_null_without_mutating_sources():
    first = {"nested": {"keep": 1, "replace": [1, 2]}, "scalar": "before"}
    second = {"nested": {"replace": [3]}, "scalar": None}
    result = deep_merge(first, second)
    assert thaw(result) == {"nested": {"keep": 1, "replace": [3]}, "scalar": None}
    second["nested"]["replace"].append(4)
    assert result["nested"]["replace"] == (3,)
    assert first["scalar"] == "before"


def test_layer_precedence_defaults_and_error_source(tmp_path):
    home, repository, instance, companion = make_home(tmp_path)
    schema = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "object",
                "properties": {
                    "keep": {"type": "integer", "default": 3},
                    "count": {"type": "integer", "default": 1},
                },
                "additionalProperties": False,
            },
            "items": {"type": "array", "items": {"type": "string"}, "default": ["default"]},
        },
        "additionalProperties": False,
    }
    update_plugin(repository, schema)
    instance["plugins"]["fake-model"] = {"config": {"nested": {"count": 2}, "items": ["instance"]}}
    companion["plugins"]["fake-model"]["config"] = {"items": ["companion"]}
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions" / "companion_a" / "companion.yaml", companion)
    report = load_home(home, repo_root=repository)
    assert report.valid, report.to_dict()
    merged = report.plugin_configs["companion_a"]["plugins"]["fake-model"]
    assert thaw(merged) == {"nested": {"keep": 3, "count": 2}, "items": ["companion"]}
    instance["plugins"]["fake-model"]["config"]["nested"]["count"] = "invalid"
    write_yaml(home / "clytie.local.yaml", instance)
    report = load_home(home, repo_root=repository)
    issues = [
        error
        for error in report.errors
        if error.pointer == "/plugins/fake-model/config/nested/count"
    ]
    assert issues and issues[0].file == str(home / "clytie.local.yaml")
    assert issues[0].actual == "invalid" and "integer" in issues[0].expected


def test_root_defaults_override_property_defaults_and_keep_missing_properties(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    update_plugin(
        repository,
        {
            "type": "object",
            "default": {"nested": {"count": 7}},
            "properties": {
                "nested": {
                    "type": "object",
                    "properties": {
                        "count": {"type": "integer", "default": 1},
                        "keep": {"type": "integer", "default": 2},
                    },
                }
            },
        },
    )
    report = load_home(home, repo_root=repository)
    assert report.valid, report.to_dict()
    merged = report.plugin_configs["companion_a"]["plugins"]["fake-model"]
    assert thaw(merged) == {"nested": {"count": 7, "keep": 2}}


def test_all_errors_include_file_pointer_expected_actual(tmp_path):
    home, repository, instance, companion = make_home(tmp_path)
    instance["timezone"] = "Invalid/Zone"
    instance["unknown_field"] = True
    instance["people"]["member"] = {"role": "owner", "accounts": []}
    companion["persona"]["scenes"].pop("public_group")
    companion["persona"]["files"]["base"] = "missing.md"
    companion["permissions"]["member"] = ["*"]
    companion["channels"][0]["account"] = 3
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions" / "companion_a" / "companion.yaml", companion)
    report = load_home(home, repo_root=repository)
    assert not report.valid and len(report.errors) >= 7
    pointers = {error.pointer for error in report.errors}
    assert {
        "/timezone",
        "/people",
        "/persona/scenes/public_group",
        "/persona/files/base",
        "/permissions/member",
        "/channels/0/account",
    } <= pointers
    assert all(
        set(error.to_dict()) == {"file", "pointer", "expected", "actual"} for error in report.errors
    )


@pytest.mark.parametrize("version", [2, "1", True])
def test_schema_version_is_not_coerced(tmp_path, version):
    home, repository, instance, _ = make_home(tmp_path)
    instance["schema_version"] = version
    write_yaml(home / "clytie.local.yaml", instance)
    report = load_home(home, repo_root=repository)
    assert any(error.pointer == "/schema_version" for error in report.errors)


def test_extensions_are_allowed_but_unknown_fields_rejected(tmp_path):
    home, repository, instance, companion = make_home(tmp_path)
    instance["x-comment"] = {"arbitrary": "comment"}
    companion["names"]["x-note"] = "comment"
    companion["boundary"] = {"x-note": "subfields await a later specification"}
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions" / "companion_a" / "companion.yaml", companion)
    assert load_home(home, repo_root=repository).valid
    companion["boundary"]["unrecognized"] = 5
    write_yaml(home / "companions" / "companion_a" / "companion.yaml", companion)
    assert any(
        error.pointer == "/boundary/unrecognized"
        for error in load_home(home, repo_root=repository).errors
    )


def test_unreadable_invalid_and_missing_files_are_reported(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    assert load_home(tmp_path / "absent", repo_root=repository).exit_code == 2
    assert load_home(tmp_path, repo_root=repository).exit_code == 1
    (home / "clytie.local.yaml").write_text(
        "schema_version: 1\nschema_version: 1\n", encoding="utf-8"
    )
    report = load_home(home, repo_root=repository)
    assert not report.valid and "unique keys" in report.errors[0].expected


def test_missing_invalid_and_ambiguous_plugins(tmp_path):
    home, repository, _, companion = make_home(tmp_path)
    companion["model"]["plugin"] = "missing-model"
    companion["plugins"]["fake-model"]["enabled"] = False
    write_yaml(home / "companions" / "companion_a" / "companion.yaml", companion)
    report = load_home(home, repo_root=repository)
    assert report.companions[0]["model"]["status"] == "missing"
    companion["model"]["plugin"] = "fake-model"
    write_yaml(home / "companions" / "companion_a" / "companion.yaml", companion)
    source = repository / "plugins" / "fake-model" / "plugin.json"
    duplicate = home / "plugins" / "fake-model" / "plugin.json"
    duplicate.parent.mkdir(parents=True)
    duplicate.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    report = load_home(home, repo_root=repository)
    assert report.companions[0]["model"]["status"] == "invalid"
    assert any("exactly one plugin manifest" in error.expected for error in report.errors)


def test_external_schema_references_are_rejected_without_fetching(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    update_plugin(
        repository, {"type": "object", "$ref": "https://schemas.example.invalid/plugin.json"}
    )
    report = load_home(home, repo_root=repository)
    assert any(error.pointer == "/config/$ref" for error in report.errors)
    assert report.companions[0]["model"]["status"] == "invalid"


def test_home_precedence(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    environment = tmp_path / "environment"
    repository = tmp_path / "repository"
    monkeypatch.setenv("CLYTIE_HOME", str(environment))
    assert resolve_home(explicit, repo_root=repository) == explicit
    assert resolve_home(repo_root=repository) == environment
    monkeypatch.delenv("CLYTIE_HOME")
    assert resolve_home(repo_root=repository) == tmp_path / "Clytie-home"


def test_missing_and_unknown_fields_have_exact_pointers(tmp_path):
    home, repository, instance, _ = make_home(tmp_path)
    instance.pop("timezone")
    instance["unexpected"] = "value"
    write_yaml(home / "clytie.local.yaml", instance)
    errors = load_home(home, repo_root=repository).errors
    assert any(error.pointer == "/timezone" and error.actual == "missing" for error in errors)
    assert any(error.pointer == "/unexpected" and error.actual == "value" for error in errors)


def test_invalid_collection_members_do_not_interrupt_error_aggregation(tmp_path):
    home, repository, instance, companion = make_home(tmp_path)
    instance["conversations"]["test_group"]["members_expected"] = [{"invalid": "actor"}]
    instance["test_conversation"] = []
    companion["persona"]["scenes"]["owner_private"] = [{"invalid": "layer"}]
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions" / "companion_a" / "companion.yaml", companion)
    report = load_home(home, repo_root=repository)
    assert not report.valid and len(report.errors) >= 3
    assert {
        "/conversations/test_group/members_expected/0",
        "/test_conversation",
        "/persona/scenes/owner_private/0",
    } <= {error.pointer for error in report.errors}


def test_unresolvable_local_ref_is_an_error_not_traceback(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    update_plugin(repository, {"type": "object", "$ref": "#/$defs/missing"})
    report = load_home(home, repo_root=repository)
    assert not report.valid
    assert any("schema" in error.expected for error in report.errors)


def test_defaults_apply_to_inline_model_config(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    update_plugin(
        repository,
        {
            "type": "object",
            "properties": {"count": {"type": "integer", "default": 2}},
            "required": ["count"],
        },
    )
    report = load_home(home, repo_root=repository)
    assert report.valid, report.to_dict()


def test_installed_package_finds_checkout_from_cwd_without_imports(tmp_path, monkeypatch):
    monkeypatch.setattr(
        configuration_module,
        "__file__",
        str(tmp_path / "installed" / "clytie" / "config" / "__init__.py"),
    )
    monkeypatch.chdir(ROOT / "tests")
    assert configuration_module.repository_root() == ROOT


def test_standalone_package_uses_bundled_schemas_and_home_plugins(tmp_path, monkeypatch):
    home, repository, _, _ = make_home(tmp_path)
    package = tmp_path / "installed" / "clytie"
    for schema in (repository / "schemas").glob("*.json"):
        target = package / "schemas" / schema.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
    for plugin in (repository / "plugins").glob("*/plugin.json"):
        target = home / "plugins" / plugin.parent.name / "plugin.json"
        target.parent.mkdir(parents=True)
        target.write_text(plugin.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(configuration_module, "__file__", str(package / "config" / "__init__.py"))
    monkeypatch.setattr(configuration_module, "files", lambda name: package)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("CLYTIE_HOME", raising=False)
    assert configuration_module.repository_root() is None
    report = load_home(home)
    assert report.valid, report.to_dict()
    unresolved = load_home()
    assert unresolved.exit_code == 2
    assert "explicit --home" in unresolved.errors[0].expected


def test_invalid_root_default_is_not_silently_discarded(tmp_path):
    home, repository, _, _ = make_home(tmp_path)
    update_plugin(repository, {"type": "object", "default": None})
    report = load_home(home, repo_root=repository)
    assert not report.valid
    assert any(
        error.pointer == "/config/default" and error.actual is None for error in report.errors
    )


@pytest.mark.parametrize(
    ("version", "valid"),
    [
        ("1.0.0", True),
        ("1.0.0-0", True),
        ("1.0.0-alpha.1+build.01", True),
        ("1.0.0-01a", True),
        ("1.0.0-01", False),
        ("1.0.0-alpha.01", False),
        ("01.0.0", False),
        ("1.0.0\n", False),
    ],
)
def test_plugin_semantic_version_format_is_validated_by_status(tmp_path, version, valid):
    home, repository, _, _ = make_home(tmp_path)
    path = repository / "plugins" / "fake-model" / "plugin.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["version"] = version
    path.write_text(json.dumps(manifest), encoding="utf-8")
    report = load_home(home, repo_root=repository)
    assert report.valid is valid, report.to_dict()
    assert report.companions[0]["model"]["status"] == ("found" if valid else "invalid")
    if not valid:
        assert any(
            error.pointer == "/version" and error.actual == version for error in report.errors
        )


@pytest.mark.parametrize(
    ("duration", "valid"),
    [
        ("PT1M", True),
        ("P1Y2M3DT4H5M6S", False),
        ("P1W", True),
        ("PT0.5S", True),
        ("PT0,5S", True),
        ("not-a-duration", False),
        ("P", False),
        ("PT", False),
        ("P1WT1H", False),
        ("PT1M\n", False),
    ],
)
def test_plugin_schedule_duration_format_is_validated_by_status(tmp_path, duration, valid):
    home, repository, _, _ = make_home(tmp_path)
    path = repository / "plugins" / "fake-model" / "plugin.json"
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["contributes"] = {
        "schedule": [{"id": "tick", "every": duration, "handler": "on_tick"}]
    }
    path.write_text(json.dumps(manifest), encoding="utf-8")
    report = load_home(home, repo_root=repository)
    assert report.valid is valid, report.to_dict()
    assert report.companions[0]["model"]["status"] == ("found" if valid else "invalid")
    if not valid:
        assert any(
            error.pointer == "/contributes/schedule/0/every" and error.actual == duration
            for error in report.errors
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("idle_minutes", 4),
        ("daily_limit", 0),
        ("window_chars", 1000),
        ("max_items", 0),
        ("retracted_similarity", -0.1),
        ("retracted_similarity", 1.1),
    ],
)
def test_recollect_schema_rejects_out_of_range(field, value):
    schema = json.loads((ROOT / "schemas/companion.schema.json").read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema["properties"]["memory"]["properties"]["recollect"])
    assert list(validator.iter_errors({field: value}))


def test_recollect_schema_defaults_and_fade_bounds():
    schema = json.loads((ROOT / "schemas/companion.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    memory = schema["properties"]["memory"]["properties"]
    recollect = memory["recollect"]["properties"]
    assert recollect["idle_minutes"]["default"] == 30
    assert recollect["retracted_similarity"]["default"] == 0.85
    assert recollect["model"]["default"] is None
    assert not list(
        Draft202012Validator(memory["fade"]).iter_errors(
            {"half_life_days": 30, "reinforce": 1.5, "max_half_life_days": 365, "freeze_below": 0.1}
        )
    )


def test_conversation_identity_includes_kind_and_rejects_exact_duplicates(tmp_path):
    home, repository, instance, _ = make_home(tmp_path)
    group = next(c for c in instance["conversations"].values() if c["kind"] == "group")
    private = dict(group, kind="private", scene="owner_private")
    instance["conversations"]["owner_chat"] = private
    private["platform_id"] = group["platform_id"]
    private["channel"] = group["channel"]
    write_yaml(home / "clytie.local.yaml", instance)
    assert load_home(home, repo_root=repository).valid
    instance["conversations"]["duplicate_group"] = dict(group)
    write_yaml(home / "clytie.local.yaml", instance)
    report = load_home(home, repo_root=repository)
    assert not report.valid
    assert "duplicate_group" in str(report.errors)
    assert "clytie.local.yaml" in str(report.errors)


@pytest.mark.parametrize("checkout", [True, False])
def test_missing_default_consolidator_schema_has_named_error(tmp_path, monkeypatch, checkout):
    from clytie import config

    monkeypatch.setattr(config, "files", lambda _: tmp_path)
    monkeypatch.setattr(config, "repository_root", lambda: tmp_path if checkout else None)
    with pytest.raises(FileNotFoundError, match=r"companion\.schema\.json"):
        config.default_consolidator.__wrapped__()
