"""No-model, fail-closed app-server preflight. Never persists raw config responses.

An evidence file is diagnostic only, never a transferable review authorization.
Thread-bound tool probes remain blocked until their execution binding is verified.
"""

import argparse
import hashlib
import json
import os
import queue
import subprocess
import threading
from pathlib import Path

from codex_review import EFFORT, MODEL, SETTINGS, client_environment
from review_protocol import ReviewIncomplete

from clytie.clock import SystemClock

NO_MODEL_METHODS = frozenset(
    {
        "initialize",
        "config/read",
        "configRequirements/read",
        "thread/start",
        "experimentalFeature/list",
        "mcpServerStatus/list",
        "app/installed",
    }
)
DISABLED_FEATURES = {
    item.split("=", 1)[0].removeprefix("features.")
    for item in SETTINGS
    if item.startswith("features.") and item.endswith("=false")
}
EXTRA_SETTINGS = [
    'model="gpt-6-astra"',
    'approval_policy="never"',
    "analytics.enabled=false",
]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def inspect_config(response, requirements):
    """Whitelist output: no values from credentials, instructions or source paths."""
    config = response.get("config", {})
    layers = response.get("layers")
    blockers = []
    if not isinstance(config, dict):
        return {"ready_for_thread": False, "blockers": ["invalid_config"]}
    if not isinstance(layers, list) or not layers:
        blockers.append("layers_missing")
        layers = []
    sources = []
    catalogs = {"mcp_servers": set(), "plugins": set()}
    for layer in layers:
        source = layer.get("name", {})
        sources.append(
            {
                "kind": source.get("type")
                if source.get("type")
                in {
                    "packagedDefaults",
                    "mdm",
                    "system",
                    "enterpriseManaged",
                    "user",
                    "project",
                    "sessionFlags",
                    "legacyManagedConfigTomlFromFile",
                    "legacyManagedConfigTomlFromMdm",
                }
                else "unknown",
                "source_sha256": digest(source),
                "disabled": bool(layer.get("disabledReason")),
                "version_sha256": digest(layer.get("version")),
            }
        )
        data = layer.get("config")
        if not isinstance(data, dict):
            blockers.append("invalid_layer")
            continue
        for category in catalogs:
            catalog = data.get(category, {})
            if isinstance(catalog, dict):
                catalogs[category].update(catalog)
            else:
                blockers.append("invalid_catalog")
    inventory = {}
    for category, names in catalogs.items():
        effective = config.get(category, {})
        if not isinstance(effective, dict):
            effective = {}
            blockers.append("invalid_catalog")
        names.update(effective)
        rows = []
        for name in sorted(names - {"_default"}):
            entry = effective.get(name)
            disabled = isinstance(entry, dict) and entry.get("enabled") is False
            rows.append({"id_sha256": digest(name), "explicitly_disabled": disabled})
            if not disabled:
                blockers.append(
                    "mcp_not_explicitly_disabled"
                    if category == "mcp_servers"
                    else "plugin_not_explicitly_disabled"
                )
        inventory[category] = rows
    features = config.get("features", {})
    if not isinstance(features, dict):
        features = {}
    feature_checks = {name: features.get(name) is False for name in sorted(DISABLED_FEATURES)}
    if not all(feature_checks.values()):
        blockers.append("feature_disable_not_verified")
    # Explicitly flag additional possible entrypoints, without treating absence as proof.
    extra_entries = {}
    for name, enabled in features.items():
        if name not in DISABLED_FEATURES and any(
            word in name for word in ("plugin", "app", "hook", "browser", "computer", "skill")
        ):
            extra_entries[digest(name)] = {
                "enabled": enabled if isinstance(enabled, bool) else None
            }
            if enabled is True and name != "skip_host_skill_discovery":
                blockers.append("additional_entrypoint_enabled")
    profile = config.get("permissions", {}).get("clytie-review", {})
    expected_fs = {":root": "deny", ":minimal": "read", ":workspace_roots": {".": "read"}}
    policy = {
        "mxc": config.get("windows", {}).get("sandbox") == "mxc",
        "profile": config.get("default_permissions") == "clytie-review",
        "filesystem": profile.get("filesystem") == expected_fs,
        "network_disabled": profile.get("network", {}).get("enabled") is False,
        "model": config.get("model") == MODEL,
        "effort": config.get("model_reasoning_effort") == EFFORT,
        "approval_never": config.get("approval_policy") == "never",
        "private_docs_disabled": config.get("project_doc_max_bytes") == 0,
        "tool_environment_none": config.get("shell_environment_policy", {}).get("inherit")
        == "none",
        "web_disabled": config.get("web_search") == "disabled",
    }
    if not all(policy.values()):
        blockers.append("effective_policy_mismatch")
    instructions = {
        name: bool(config.get(name))
        for name in (
            "instructions",
            "developer_instructions",
            "model_instructions_file",
            "experimental_instructions_file",
            "compact_prompt",
            "notify",
        )
    }
    if any(instructions.values()):
        blockers.append("inherited_instructions_or_notify")
    req = requirements.get("requirements")
    req_summary = {"present": isinstance(req, dict)}
    if "requirements" not in requirements:
        blockers.append("requirements_response_missing")
    elif req is None:
        req_summary["state"] = "none_published"
    elif not isinstance(req, dict):
        blockers.append("requirements_response_invalid")
    else:
        req_summary["sha256"] = digest(req)
        req_summary["additional_instructions_present"] = bool(
            req.get("additionalDeveloperInstructions")
        )
        if req_summary["additional_instructions_present"]:
            blockers.append("managed_instructions_not_isolated")
        req_summary["policy_fields_present"] = sorted(
            name
            for name in (
                "allowedPermissionProfiles",
                "allowedWindowsSandboxImplementations",
                "featureRequirements",
                "allowBrowserAndComputerUse",
                "allowRemoteControl",
                "allowManagedHooksOnly",
                "allowLoginShell",
            )
            if name in req
        )
    # Config catalogs alone do not enumerate installed plugin hooks/skills/remote discovery.
    blockers.append("plugin_entrypoint_inventory_not_verified")
    return {
        "ready_for_thread": not blockers,
        "blockers": sorted(set(blockers)),
        "policy": policy,
        "features_disabled": feature_checks,
        "additional_entrypoints": extra_entries,
        "instruction_presence": instructions,
        "layers": sources,
        "origins_sha256": digest(response.get("origins")),
        "inventory": inventory,
        "requirements": req_summary,
    }


def all_pages(rpc, method, thread_id):
    rows, seen = [], set()
    cursor = None
    while True:
        result = rpc(method, {"threadId": thread_id, "cursor": cursor, "limit": 100})
        if not isinstance(result.get("data"), list) or "nextCursor" not in result:
            raise ReviewIncomplete("incomplete pagination")
        rows.extend(result["data"])
        cursor = result["nextCursor"]
        if cursor is None:
            return rows
        if not isinstance(cursor, str) or cursor in seen:
            raise ReviewIncomplete("invalid pagination")
        seen.add(cursor)
        if len(seen) >= 100:
            raise ReviewIncomplete("pagination bound exceeded")


def inspect_runtime(features, servers, apps):
    known = {row["name"]: row.get("enabled") for row in features}
    feature_checks = {name: known.get(name) is False for name in sorted(DISABLED_FEATURES)}
    rows = []
    for row in servers:
        rows.append(
            {
                "id_sha256": digest(row.get("name")),
                "disabled": row.get("runtimeStatus") == "disabled",
                "tools_empty": row.get("tools") == {},
                "discovery_ok": "toolsError" in row and row["toolsError"] is None,
            }
        )
    installed = [
        {
            "id_sha256": digest(row.get("id")),
            "disabled": row.get("enabled") is False,
            "uncallable": row.get("callable") is False,
        }
        for row in apps.get("apps", [])
    ]
    passed = (
        all(feature_checks.values())
        and isinstance(apps.get("apps"), list)
        and all(r["disabled"] and r["tools_empty"] and r["discovery_ok"] for r in rows)
        and all(r["disabled"] and r["uncallable"] for r in installed)
    )
    return {"passed": passed, "features": feature_checks, "mcp": rows, "apps": installed}


class NoModelServer:
    """One stdio instance. Raw responses remain in memory; stderr is only hashed."""

    def __init__(self, args, cwd, *, clock=None):
        self.clock = clock or SystemClock()
        self.process = subprocess.Popen(
            args,
            cwd=cwd,
            env=client_environment(os.environ),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.messages = queue.Queue()
        self.events = []
        self.next_id = 0
        self.error_hash = hashlib.sha256()
        self.error_bytes = 0
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.errors = threading.Thread(target=self._errors, daemon=True)
        self.reader.start()
        self.errors.start()

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    self.messages.put({"invalid_json": True})
        finally:
            self.messages.put({"eof": True})

    def _errors(self):
        while chunk := self.process.stderr.read(4096):
            self.error_hash.update(chunk)
            self.error_bytes += len(chunk)

    def send(self, value):
        self.process.stdin.write(json.dumps(value).encode() + b"\n")
        self.process.stdin.flush()

    def rpc(self, method, params=None):
        if method not in NO_MODEL_METHODS:
            raise ReviewIncomplete("method not authorized in no-model preflight")
        self.next_id += 1
        request_id = self.next_id
        self.send({"id": request_id, "method": method, "params": params})
        event = {"method": method, "status": "pending"}
        self.events.append(event)
        deadline = self.clock.monotonic() + 30
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - self.clock.monotonic()))
            except queue.Empty:
                event["status"] = "timeout"
                raise ReviewIncomplete("metadata request timeout") from None
            if "eof" in message or "invalid_json" in message:
                event["status"] = "transport_failed"
                raise ReviewIncomplete("metadata transport failed")
            if message.get("id") == request_id:
                if "error" in message:
                    event["status"] = "error"
                    event["error_sha256"] = digest(message["error"])
                    raise ReviewIncomplete("metadata request rejected")
                event["status"] = "ok"
                return message["result"]
            # Never execute server-requested tools/approvals or publish their arguments.
            if "id" in message:
                self.send(
                    {
                        "id": message["id"],
                        "error": {"code": -(32 * 1000 + 601), "message": "Disabled"},
                    }
                )
                event["status"] = "unexpected_server_request"
                raise ReviewIncomplete("unexpected server request")
            self.events.append({"notification_sha256": digest(message.get("method"))})

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            self.process.wait(timeout=10)
        self.reader.join(timeout=2)
        self.errors.join(timeout=2)
        return {
            "exit_code": self.process.returncode,
            "stderr_bytes": self.error_bytes,
            "stderr_sha256": self.error_hash.hexdigest(),
        }


def preflight(cli, candidate, destination):
    destination.mkdir(parents=True, exist_ok=False)
    cli = cli.resolve(strict=True)
    snapshot = (candidate / "snapshot").resolve(strict=True)
    version = (
        subprocess.run([str(cli), "--version"], capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )
    metadata = json.loads((candidate / "metadata.json").read_text(encoding="utf-8"))
    settings = SETTINGS + EXTRA_SETTINGS
    args = [str(cli), "--no-daemon", "app-server", "--stdio", "--strict-config"]
    for value in settings:
        args.extend(["-c", value])
    evidence = {
        "status": "review_incomplete",
        "formal_rounds_consumed": 0,
        "model_calls": 0,
        "thread_created": False,
        "head": metadata["head"],
        "cli": {
            "path": str(cli),
            "version": version,
            "sha256": hashlib.sha256(cli.read_bytes()).hexdigest(),
        },
        "args": args,
        "snapshot_sha256": digest(str(snapshot)),
        "client_environment_names": sorted(client_environment(os.environ)),
        "ignore_user_config_supported": False,
        "raw_config_saved": False,
        "reusable_certificate": False,
    }
    server = None
    try:
        if version != metadata["cli_version"]:
            raise ReviewIncomplete("CLI version differs from candidate")
        server = NoModelServer(args, snapshot)
        server.rpc(
            "initialize",
            {
                "clientInfo": {"name": "clytie_review_preflight", "version": "1"},
                "capabilities": {"experimentalApi": True},
            },
        )
        server.send({"method": "initialized", "params": {}})
        config = server.rpc("config/read", {"cwd": str(snapshot), "includeLayers": True})
        requirements = server.rpc("configRequirements/read")
        evidence["config"] = inspect_config(config, requirements)
        del config, requirements
        if not evidence["config"]["ready_for_thread"]:
            raise ReviewIncomplete("pre_thread_configuration_gate")
        # Not reachable until every pre-thread entrypoint has positive disable evidence.
        result = server.rpc(
            "thread/start",
            {
                "cwd": str(snapshot),
                "ephemeral": True,
                "model": MODEL,
                "approvalPolicy": "never",
                "config": {"model_reasoning_effort": EFFORT},
            },
        )
        thread = result["thread"]
        evidence["thread_created"] = True
        evidence["thread_id_sha256"] = digest(thread["id"])
        if (
            not thread.get("ephemeral")
            or thread.get("model") != MODEL
            or thread.get("reasoningEffort") != EFFORT
        ):
            raise ReviewIncomplete("effective_thread_mismatch")
        features = all_pages(server.rpc, "experimentalFeature/list", thread["id"])
        servers = all_pages(server.rpc, "mcpServerStatus/list", thread["id"])
        apps = server.rpc("app/installed", {"threadId": thread["id"], "forceRefresh": False})
        evidence["runtime"] = inspect_runtime(features, servers, apps)
        if not evidence["runtime"]["passed"]:
            raise ReviewIncomplete("thread_runtime_gate")
        raise ReviewIncomplete("thread_bound_tool_probe_interface_unverified")
    except ReviewIncomplete as exc:
        evidence["stopped_at"] = str(exc)
    finally:
        if server is not None:
            evidence["process"] = server.close()
            evidence["events"] = server.events
        evidence["instance_closed_preflight_invalidated"] = True
        with (destination / "evidence.json").open("x", encoding="utf-8") as output:
            json.dump(evidence, output, ensure_ascii=False, indent=2)
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    options = parser.parse_args()
    result = preflight(options.cli, options.candidate, options.evidence)
    print(
        json.dumps(
            {
                "status": result["status"],
                "stage": result.get("stopped_at"),
                "thread_created": result["thread_created"],
                "model_calls": 0,
            }
        )
    )
    raise SystemExit(1)  # Metadata transport success is not a completed preflight.
