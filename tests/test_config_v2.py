"""账号引用、四层合并及补充清单的离线验收。"""

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from clytie.config import load_home

from .test_config import ROOT, make_home, update_plugin, write_yaml


def test_four_layers_and_independent_channel_accounts(tmp_path):
    home, repo, instance, companion = make_home(tmp_path)
    schema = {
        "type": "object",
        "properties": {
            "nested": {
                "type": "object",
                "properties": {
                    "default": {"default": 1},
                    "instance": {},
                    "companion": {},
                    "inline": {},
                },
            },
            "items": {"type": "array", "default": ["default"]},
            "required_inline": {"type": "string"},
        },
        "required": ["required_inline"],
    }
    update_plugin(repo, schema)
    instance["plugins"] = {
        "fake-model": {"config": {"nested": {"instance": 2}, "items": ["instance"]}}
    }
    companion["plugins"]["fake-model"]["config"] = {
        "nested": {"companion": 3},
        "items": ["companion"],
    }
    companion["model"]["config"] = {
        "nested": {"inline": 4},
        "items": ["inline"],
        "required_inline": "present",
    }
    channel_manifest = repo / "plugins/fake-channel/plugin.json"
    manifest = json.loads(channel_manifest.read_text(encoding="utf-8"))
    manifest["config"] = {
        "type": "object",
        "properties": {"account_setting": {"type": "integer", "default": 1}},
        "additionalProperties": False,
    }
    channel_manifest.write_text(json.dumps(manifest), encoding="utf-8")
    instance["accounts"]["second_bot"] = {"channel": "fake-channel", "account": "1000000004"}
    companion["channels"][0]["config"] = {"account_setting": 2}
    companion["channels"].append(
        {"plugin": "fake-channel", "account": "second_bot", "config": {"account_setting": 3}}
    )
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    assert report.valid, report.to_dict()
    config = report.plugin_configs["companion_a"]
    assert dict(config["model"]["nested"]) == {
        "default": 1,
        "instance": 2,
        "companion": 3,
        "inline": 4,
    }
    assert config["model"]["items"] == ("inline",)
    assert config["channels"][0]["account_setting"] == 2
    assert config["channels"][1]["account_setting"] == 3
    with pytest.raises(TypeError):
        config["model"]["nested"]["inline"] = 5
    companion["model"]["config"]["required_inline"] = False
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    assert any(
        e.pointer == "/model/config/required_inline" and e.file.endswith("companion.yaml")
        for e in report.errors
    )


@pytest.mark.parametrize("problem", ["missing", "channel", "disabled", "duplicate"])
def test_accounts_must_exist_match_channel_and_not_be_shared(tmp_path, problem):
    home, repo, instance, companion = make_home(tmp_path)
    expected = "/channels/0/account"
    if problem == "missing":
        companion["channels"][0]["account"] = "missing_bot"
    if problem == "channel":
        instance["accounts"]["example_bot"]["channel"] = "other-channel"
    if problem == "disabled":
        companion["plugins"]["fake-channel"]["enabled"] = False
        expected = "/plugins/fake-channel/enabled"
    if problem == "duplicate":
        second = copy.deepcopy(companion)
        second["id"] = "companion_b"
        instance["companions"].append("companion_b")
        write_yaml(home / "companions/companion_b/companion.yaml", second)
        persona = home / "companions/companion_b/persona/base.md"
        persona.parent.mkdir()
        persona.write_text("Fictional companion.", encoding="utf-8")
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    assert not report.valid
    assert any(e.pointer == expected for e in report.errors)
    if problem == "duplicate":
        assert not any(companion["valid"] for companion in report.companions)


def test_role_plugins_are_enabled_without_plugin_settings(tmp_path):
    home, repo, _, companion = make_home(tmp_path)
    companion["plugins"] = {}
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    assert report.valid, report.to_dict()
    assert {item["id"] for item in report.companions[0]["plugins"]} == {
        "fake-model",
        "fake-channel",
    }


@pytest.mark.parametrize(
    "relative", ["missing.txt", "../outside.txt", "/outside.txt", "C:\\outside.txt"]
)
def test_secret_paths_must_exist_and_stay_inside_secrets(tmp_path, relative):
    home, repo, instance, _ = make_home(tmp_path)
    instance["accounts"]["example_bot"]["secret"] = relative
    write_yaml(home / "clytie.local.yaml", instance)
    report = load_home(home, repo_root=repo)
    assert any(e.pointer == "/accounts/example_bot/secret" for e in report.errors)


def test_secret_existence_never_reads_content(tmp_path, monkeypatch):
    home, repo, instance, _ = make_home(tmp_path)
    secret_file = home / "secrets/fictional.txt"
    secret_file.parent.mkdir()
    secret_file.write_text("fictional unread sentinel", encoding="utf-8")
    instance["accounts"]["example_bot"]["secret"] = "fictional.txt"
    write_yaml(home / "clytie.local.yaml", instance)
    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path != secret_file
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    report = load_home(home, repo_root=repo)
    assert report.valid, report.to_dict()


def test_boundary_context_defaults_and_precise_errors(tmp_path):
    home, repo, _, companion = make_home(tmp_path)
    report = load_home(home, repo_root=repo)
    config = report.plugin_configs["companion_a"]["config"]
    assert config["boundary"]["refusal_threshold"] == 5
    assert all(config["boundary"]["categories"].values())
    assert config["context"]["max_chars"] == 20_000
    companion["boundary"] = {"categories": {"romance": False}, "refusal_threshold": 0}
    companion["context"] = {"max_chars": 200_001, "unknown": True}
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    pointers = {e.pointer for e in report.errors}
    assert {"/boundary/refusal_threshold", "/context/max_chars", "/context/unknown"} <= pointers


def contributions():
    return {
        "channel": {"factory": "create_channel", "platform": "fictional"},
        "model": {"factory": "create_model"},
        "embedder": {"factory": "create_embedder", "dimension": 3},
        "notifier": {"factory": "create_notifier"},
        "memory": {
            "retriever": {"factory": "create_retriever"},
            "importer": {"factory": "create_importer", "formats": ["fictional"]},
            "consolidator": {"factory": "create_consolidator"},
        },
        "tools": [
            {
                "factory": "create_tool",
                "name": "fake",
                "description": "fictional",
                "when_not_to_use": "discussion",
                "examples": [],
                "parameters": {},
                "audience": ["owner"],
                "heavy": False,
                "risk": "low",
                "delivery": "text",
            }
        ],
        "services": [
            {"name": "fake.state", "version": "1.0", "methods": ["get"], "handler": "on_service"}
        ],
        "events": {
            "subscribe": ["task.completed"],
            "publish": ["fake.changed"],
            "handler": "on_event",
        },
        "schedule": [{"id": "daily", "at": "09:00", "handler": "on_tick"}],
        "cli_commands": [{"name": "fake", "help": "fictional", "args": {}, "handler": "on_cli"}],
        "api_routes": [
            {
                "method": "GET",
                "path": "/plugins/fake/status",
                "audit": False,
                "handler": "on_status",
            }
        ],
        "webui_panels": [
            {
                "id": "status",
                "title": "fictional",
                "kind": "kv",
                "data_route": "/plugins/fake/status",
            }
        ],
    }


def test_all_manifest_encodings_validate_and_reject_invalid_fields():
    schema = json.loads((ROOT / "schemas/plugin.schema.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "plugins/fake-model/plugin.json").read_text(encoding="utf-8"))
    manifest["contributes"] = contributions()
    validator = Draft202012Validator(schema)
    assert list(validator.iter_errors(manifest)) == []
    bad_cases = [
        ("model", {"factory": "create.model"}),
        ("channel", {"factory": "create_channel"}),
        ("embedder", {"factory": "create_embedder", "dimension": 0}),
        ("schedule", [{"id": "daily", "at": "09:00", "days": [8], "handler": "on_tick"}]),
        ("events", {"subscribe": ["task.completed"]}),
        (
            "api_routes",
            [
                {
                    "method": "POST",
                    "path": "/plugins/fake/action",
                    "audit": False,
                    "handler": "on_action",
                }
            ],
        ),
    ]
    for name, value in bad_cases:
        broken = copy.deepcopy(manifest)
        broken["contributes"][name] = value
        assert list(validator.iter_errors(broken)), name
