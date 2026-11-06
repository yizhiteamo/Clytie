"""No-model review preflight must fail closed before creating a thread."""

import importlib.util
import json
import sys
from copy import deepcopy
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
    assert "thread/shellCommand" not in review.NO_MODEL_METHODS


def valid_config():
    return {
        "config": {
            "features": {
                **dict.fromkeys(review.DISABLED_FEATURES, False),
                "skip_host_skill_discovery": True,
            },
            "mcp_servers": {"fictional.server": {"enabled": False}},
            "plugins": {"_default": {"enabled": False}, "fictional.plugin": {"enabled": False}},
            "skills": {"bundled": {"enabled": False}, "include_instructions": False},
            "notify": [],
            "windows": {"sandbox": "mxc"},
            "default_permissions": "clytie-review",
            "permissions": {
                "clytie-review": {
                    "filesystem": {
                        ":root": "deny",
                        ":minimal": "read",
                        ":workspace_roots": {".": "read"},
                        "glob_scan_max_depth": None,
                    },
                    "network": {"enabled": False},
                }
            },
            "model": "gpt-6-astra",
            "model_reasoning_effort": "high",
            "approval_policy": "never",
            "project_doc_max_bytes": 0,
            "shell_environment_policy": {
                "inherit": "none",
                "set": {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows", "SystemDrive": "C:"},
            },
            "web_search": "disabled",
        },
        "layers": [{"name": {"type": "sessionFlags"}, "version": "fictional", "config": {}}],
        "origins": {},
    }


def valid_runtime():
    return (
        [{"name": name, "enabled": False} for name in review.DISABLED_FEATURES],
        [
            {
                "name": "fictional.server",
                "runtimeStatus": "disabled",
                "tools": {},
                "toolsError": None,
            }
        ],
        {"apps": [{"id": "fictional", "enabled": False, "callable": False}]},
    )


def test_complete_config_and_runtime_positive():
    assert review.inspect_config(valid_config(), {"requirements": None})["ready_for_thread"]
    assert review.inspect_runtime(*valid_runtime())["passed"]


def test_managed_policy_cannot_be_bypassed():
    for requirement in (
        {"featureRequirements": {"plugins": True}},
        {"allowedWindowsSandboxImplementations": ["elevated"]},
        {"allowedPermissionProfiles": {"clytie-review": False}},
    ):
        value = review.inspect_config(valid_config(), {"requirements": requirement})
        assert "managed_policy_conflict" in value["blockers"]
        assert not value["ready_for_thread"]


@pytest.mark.parametrize(
    "condition",
    [
        "mcp",
        "plugin",
        "feature",
        "notify",
        "root",
        "minimal",
        "write",
        "extra_root",
        "extends",
        "extra_path",
        "glob_depth",
        "unknown_fs",
        "network",
        "runtime_roots",
        "skills",
    ],
)
def test_one_config_condition_rejected_from_complete_valid_input(condition):
    response = valid_config()
    config = response["config"]
    profile = config["permissions"]["clytie-review"]
    fs = profile["filesystem"]
    if condition == "mcp":
        config["mcp_servers"]["fictional.server"]["enabled"] = True
    elif condition == "plugin":
        config["plugins"]["fictional.plugin"]["enabled"] = True
    elif condition == "feature":
        config["features"]["remote_plugin"] = True
    elif condition == "notify":
        config["notify"] = ["fictional"]
    elif condition == "root":
        fs[":root"] = "read"
    elif condition == "minimal":
        fs[":minimal"] = "write"
    elif condition == "write":
        fs[":workspace_roots"]["."] = "write"
    elif condition == "extra_root":
        fs[":workspace_roots"]["../fictional"] = "read"
    elif condition == "extends":
        profile["extends"] = "fictional"
    elif condition == "extra_path":
        fs["/fictional"] = "read"
    elif condition == "glob_depth":
        fs["glob_scan_max_depth"] = 1
    elif condition == "unknown_fs":
        fs["future_option"] = None
    elif condition == "network":
        profile["network"]["enabled"] = True
    elif condition == "runtime_roots":
        config["runtime_workspace_roots"] = ["/fictional"]
    elif condition == "skills":
        config["skills"]["bundled"]["enabled"] = True
    assert not review.inspect_config(response, {"requirements": None})["ready_for_thread"]


@pytest.mark.parametrize(
    "condition",
    [
        "initialization",
        "uninitialized",
        "discovery",
        "callable",
        "feature",
        "missing_server",
        "tools",
    ],
)
def test_one_runtime_condition_rejected_from_complete_valid_input(condition):
    features, servers, apps = deepcopy(valid_runtime())
    if condition == "initialization":
        servers[0]["runtimeStatus"] = "failed"
    elif condition == "uninitialized":
        servers[0]["runtimeStatus"] = "notStarted"
    elif condition == "discovery":
        servers[0]["toolsError"] = "fictional failure"
    elif condition == "callable":
        apps["apps"][0]["callable"] = True
    elif condition == "feature":
        features.append({"name": "fictional_plugin_entry", "enabled": True})
    elif condition == "missing_server":
        servers.clear()
    elif condition == "tools":
        servers[0]["tools"]["fictional"] = {}
    assert not review.inspect_runtime(features, servers, apps, [review.digest("fictional.server")])[
        "passed"
    ]


def test_safe_toml_identifiers_roundtrip_and_requirement_independence(monkeypatch):
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    names = [
        "fictional.name",
        'fictional"quote',
        "fictional\\path",
        "fictional\nline",
        "fictional $() ] 😀",
    ]
    response = {
        "config": {
            "mcp_servers": dict.fromkeys(names, {"enabled": True}),
            "plugins": dict.fromkeys(names, {"enabled": True}),
        }
    }
    overrides = review.disable_catalogs(response)
    # This CLI splits the key portion itself; a TOML parser accepting a quoted
    # dotted assignment alone is insufficient. Only fixed top-level keys go there.
    assert {arg.partition("=")[0] for arg in overrides} == {"mcp_servers", "plugins"}
    parsed = tomllib.loads("\n".join(overrides))
    assert set(parsed["mcp_servers"]) == set(names)
    assert set(parsed["plugins"]) == set(names) | {"_default"}
    assert parsed["plugins"]["_default"]["enabled"] is False
    assert all(v["enabled"] is False for group in parsed.values() for v in group.values())
    monkeypatch.setattr(review, "SETTINGS", [])
    monkeypatch.setattr(review, "EXTRA_SETTINGS", [])
    response = valid_config()
    del response["config"]["features"]["remote_plugin"]
    assert "remote_plugin" in review.DISABLED_FEATURES
    assert not review.inspect_config(response, {"requirements": None})["ready_for_thread"]


def test_snapshot_content_hash_not_path_hash(tmp_path):
    root = tmp_path / "snapshot"
    root.mkdir()
    path = root / "fictional.txt"
    path.write_text("before", encoding="utf-8")
    before = review.snapshot_manifest(root)
    path.write_text("after", encoding="utf-8")
    after = review.snapshot_manifest(root)
    assert before["sha256"] != after["sha256"]
    assert after["files"][0]["path"] == "fictional.txt"


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


def test_pagination_second_page_missing_cursor_rejected():
    features = valid_runtime()[0]

    def rpc(method, params):
        if params["cursor"] is None:
            return {"data": features[:1], "nextCursor": "second"}
        return {"data": features[1:]}

    with pytest.raises(review.ReviewIncomplete, match="pagination"):
        review.all_pages(rpc, "experimentalFeature/list", "fictional-thread")


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
    manifest = review.snapshot_manifest(candidate / "snapshot")
    (candidate / "snapshot-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (candidate / "metadata.json").write_text(
        json.dumps(
            {
                "cli_version": "fictional-cli",
                "head": "a" * 40,
                "snapshot_manifest_sha256": manifest["sha256"],
            }
        ),
        encoding="utf-8",
    )
    cli = tmp_path / "fictional.exe"
    cli.write_bytes(b"fictional executable")
    value = json.loads((candidate / "metadata.json").read_text(encoding="utf-8"))
    value["cli_sha256"] = review.hashlib.sha256(cli.read_bytes()).hexdigest()
    value["cli_path_sha256"] = review.hashlib.sha256(str(cli.resolve()).encode()).hexdigest()
    (candidate / "metadata.json").write_text(json.dumps(value), encoding="utf-8")
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
    assert calls == [
        "initialize",
        "config/read",
        "initialize",
        "config/read",
        "configRequirements/read",
    ]
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


@pytest.mark.parametrize(
    "change", [None, "new_instance_config", "post_thread_config", "initialization_failure"]
)
def test_same_instance_metadata_positive_and_no_diagnostic_proof_reuse(
    tmp_path, monkeypatch, change
):
    from types import SimpleNamespace

    candidate = tmp_path / "candidate"
    snapshot = candidate / "snapshot"
    snapshot.mkdir(parents=True)
    (snapshot / "fictional.txt").write_text("fictional", encoding="utf-8")
    manifest = review.snapshot_manifest(snapshot)
    (candidate / "snapshot-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    cli = tmp_path / "fictional.exe"
    cli.write_bytes(b"fictional")
    (candidate / "metadata.json").write_text(
        json.dumps(
            {
                "head": "a" * 40,
                "cli_version": "fictional",
                "cli_sha256": review.hashlib.sha256(cli.read_bytes()).hexdigest(),
                "cli_path_sha256": review.hashlib.sha256(str(cli.resolve()).encode()).hexdigest(),
                "snapshot_manifest_sha256": manifest["sha256"],
            }
        ),
        encoding="utf-8",
    )
    instances = []

    class Server:
        def __init__(self, args, cwd):
            self.args, self.calls, self.events, self.reads = args, [], [], 0
            instances.append(self)

        def send(self, value):
            assert value["method"] == "initialized"

        def rpc(self, method, params=None):
            assert method in review.NO_MODEL_METHODS
            self.calls.append(method)
            if method == "initialize" and change == "initialization_failure":
                raise review.ReviewIncomplete("fictional initialization failure")
            if method == "config/read":
                self.reads += 1
                value = valid_config()
                if (len(instances) == 2 and change == "new_instance_config") or (
                    change == "post_thread_config" and self.reads == 2
                ):
                    value["config"]["mcp_servers"]["fictional.server"]["enabled"] = True
                return value
            if method == "configRequirements/read":
                return {"requirements": None}
            if method == "thread/start":
                assert len(instances) == 2 and params["ephemeral"]
                assert params["allowProviderModelFallback"] is False
                return {
                    "thread": {"id": "fictional-thread", "ephemeral": True},
                    "model": "gpt-6-astra",
                    "reasoningEffort": "high",
                    "runtimeWorkspaceRoots": [str(snapshot.resolve())],
                    "activePermissionProfile": {"id": "clytie-review", "extends": None},
                }
            if method in {"experimentalFeature/list", "mcpServerStatus/list", "app/installed"}:
                assert params["threadId"] == "fictional-thread" and self is instances[1]
                features, servers, apps = valid_runtime()
                if method == "app/installed":
                    assert params["forceRefresh"] is False
                    return apps
                data = features if method == "experimentalFeature/list" else servers
                return {"data": data, "nextCursor": None}
            return {}

        def close(self):
            return {"exit_code": 0}

    monkeypatch.setattr(review, "NoModelServer", Server)
    monkeypatch.setattr(
        review.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout=b"fictional")
    )
    result = review.preflight(cli, candidate, tmp_path / "output")
    assert "thread/start" not in instances[0].calls
    assert result["formal_rounds_consumed"] == result["model_calls"] == 0
    if change is None:
        assert result["runtime"]["passed"] and result["configuration_unchanged"]
        assert result["stopped_at"] == "thread_bound_tool_probe_interface_unverified"
    elif change == "new_instance_config":
        assert not result["thread_created"]
        assert result["stopped_at"] == "pre_thread_configuration_gate"
    elif change == "post_thread_config":
        assert not result["configuration_unchanged"]
        assert result["stopped_at"] == "configuration changed during preflight"
    else:
        assert not result["thread_created"] and len(instances) == 1
    assert result["status"] == "review_incomplete" and not result["reusable_certificate"]
