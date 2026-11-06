"""No-model review preflight must fail closed before creating a thread."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def load(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


load("review_protocol")
load("codex_review")
review = load("review_app_server")


def test_no_model_rpc_allowlist_and_same_instance():
    assert "turn/start" not in review.NO_MODEL_METHODS
    assert "thread/resume" not in review.NO_MODEL_METHODS
    assert "thread/fork" not in review.NO_MODEL_METHODS
    assert "config/value/write" not in review.NO_MODEL_METHODS
    assert "command/exec" not in review.NO_MODEL_METHODS


def test_empty_maps_are_not_disable_proof():
    value = {"config": {"mcp_servers": {}, "plugins": {"_default": {"enabled": False}}}}
    result = review.inspect_config(value, {})
    assert not result["ready_for_thread"]
    assert "layers_missing" in result["blockers"]


def test_sanitizer_never_copies_credentials_instructions_or_paths():
    private_marker = "fictional-private-marker"
    response = {
        "config": {
            "developer_instructions": private_marker,
            "mcp_servers": {
                private_marker: {"enabled": True, "headers": {"value": private_marker}}
            },
            "plugins": {private_marker: {"enabled": True}},
        },
        "layers": [{"name": {"type": "user", "file": private_marker}, "config": private_marker}],
        "origins": {"model": {"file": private_marker}},
    }
    result = review.inspect_config(response, {"requirements": {"modelProviders": private_marker}})
    assert private_marker not in json.dumps(result)
    assert not result["ready_for_thread"]
    assert "mcp_not_explicitly_disabled" in result["blockers"]


def test_pagination_rejects_repeated_or_missing_cursor():
    for value in ({"data": []}, {"data": [], "nextCursor": "same"}):
        with pytest.raises(review.ReviewIncomplete):
            review.all_pages(lambda *args: value, "method", "thread")


def test_pagination_all_pages_use_same_thread():
    calls = []

    def rpc(method, params):
        calls.append((method, params))
        return {"data": [len(calls)], "nextCursor": "next" if len(calls) == 1 else None}

    assert review.all_pages(rpc, "method", "thread") == [1, 2]
    assert all(params["threadId"] == "thread" for _, params in calls)


@pytest.mark.parametrize("state", [None, "starting", "failed"])
def test_mcp_missing_runtime_or_discovery_error_is_not_disabled(state):
    rows = [{"name": "fictional", "runtimeStatus": state, "tools": {}, "toolsError": None}]
    assert not review.inspect_runtime([], rows, {"apps": []})["passed"]


def test_callable_apps_and_tools_fail_even_if_disabled_flag():
    result = review.inspect_runtime(
        [],
        [
            {
                "name": "fictional",
                "runtimeStatus": "disabled",
                "tools": {"x": {}},
                "toolsError": None,
            }
        ],
        {"apps": [{"id": "fictional", "enabled": False, "callable": True}]},
    )
    assert not result["passed"]


def test_runtime_missing_feature_inventory_is_not_proof():
    assert not review.inspect_runtime([], [], {"apps": []})["passed"]


def test_requirements_null_means_none_published_not_transport_failure():
    result = review.inspect_config({"config": {}}, {"requirements": None})
    assert result["requirements"]["state"] == "none_published"
    assert "requirements_response_missing" not in result["blockers"]
    missing = review.inspect_config({"config": {}}, {})
    assert "requirements_response_missing" in missing["blockers"]


def test_preflight_stops_before_thread_and_preserves_only_filtered_evidence(tmp_path, monkeypatch):
    from types import SimpleNamespace

    marker = "fictional-private-marker"
    candidate = tmp_path / "candidate"
    (candidate / "snapshot").mkdir(parents=True)
    (candidate / "metadata.json").write_text(
        json.dumps({"cli_version": "fictional-cli", "head": "a" * 40}), encoding="utf-8"
    )
    cli = tmp_path / "fictional.exe"
    cli.write_bytes(b"fictional executable")
    calls = []
    commands = []

    class Server:
        events = []

        def __init__(self, args, cwd):
            commands.append(args)
            assert cwd == (candidate / "snapshot").resolve()

        def send(self, value):
            assert value["method"] == "initialized"

        def rpc(self, method, params=None):
            calls.append(method)
            if method == "config/read":
                assert params["includeLayers"] is True
                return {"config": {"developer_instructions": marker}}
            return {}

        def close(self):
            return {"exit_code": 0}

    monkeypatch.setattr(review, "NoModelServer", Server)
    monkeypatch.setattr(
        review.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"fictional-cli")
    )
    output = tmp_path / "evidence"
    result = review.preflight(cli, candidate, output)
    assert calls == ["initialize", "config/read", "configRequirements/read"]
    assert not result["thread_created"] and result["formal_rounds_consumed"] == 0
    assert result["instance_closed_preflight_invalidated"]
    assert not result["reusable_certificate"]
    assert marker not in (output / "evidence.json").read_text(encoding="utf-8")
    args = commands[0]
    assert args[0] == str(cli.resolve())
    assert "app-server" in args and "--strict-config" in args
    assert "--ignore-user-config" not in args
    assert 'windows.sandbox="mxc"' in args
    assert 'model="gpt-6-astra"' in args
    assert 'model_reasoning_effort="high"' in args
    assert "permissions.clytie-review.network.enabled=false" in args
    assert not (tmp_path / "rounds.json").exists()
