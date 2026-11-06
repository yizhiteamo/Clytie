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

from codex_review import EFFORT, MODEL, SETTINGS, client_environment, snapshot_manifest
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
# Requirements are deliberately independent of launch switches. Removing a switch
# must never remove the corresponding acceptance check.
DISABLED_FEATURES = frozenset(
    {
        "apps",
        "multi_agent",
        "multi_agent_v2",
        "hooks",
        "memories",
        "image_generation",
        "view_image",
        "browser_use",
        "browser_use_external",
        "in_app_browser",
        "in_app_chat",
        "tool_suggest",
        "skill_search",
        "plugins",
        "computer_use",
        "in_app_local_automation",
        "remote_plugin",
        "plugin_discovery_prefetch",
        "skill_mcp_dependency_install",
    }
)
EXTRA_SETTINGS = [
    'model="gpt-6-astra"',
    'approval_policy="never"',
    "analytics.enabled=false",
    "features.plugins=false",
    "features.computer_use=false",
    "features.in_app_local_automation=false",
    "features.remote_plugin=false",
    "features.plugin_discovery_prefetch=false",
    "features.skill_mcp_dependency_install=false",
    "notify=[]",
]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def catalog_names(response):
    result = {"mcp_servers": set(), "plugins": set()}
    configs = [response.get("config", {})]
    configs.extend(layer.get("config", {}) for layer in response.get("layers", []))
    for config in configs:
        if not isinstance(config, dict):
            raise ReviewIncomplete("invalid catalog layer")
        for category in result:
            catalog = config.get(category, {})
            if not isinstance(catalog, dict):
                raise ReviewIncomplete("invalid catalog")
            result[category].update(catalog)
    result["plugins"].discard("_default")
    return result


def disable_catalogs(response):
    """Quoted keys belong inside TOML values, not the CLI's dotted-key parser."""
    return [
        category
        + "={"
        + ",".join(
            json.dumps(name, ensure_ascii=False) + "={enabled=false}"
            for name in sorted(names | ({"_default"} if category == "plugins" else set()))
        )
        + "}"
        for category, names in catalog_names(response).items()
    ]


def permission_checks(config):
    permissions = config.get("permissions", {})
    profile = permissions.get("clytie-review", {}) if isinstance(permissions, dict) else {}
    if not isinstance(profile, dict):
        profile = {}
    filesystem = profile.get("filesystem")
    if isinstance(filesystem, dict):
        filesystem = dict(filesystem)
        # This version emits a nullable traversal-depth option even when unset.
        # Only this exact null field is semantically inert; all other differences fail.
        if "glob_scan_max_depth" in filesystem and filesystem["glob_scan_max_depth"] is None:
            filesystem.pop("glob_scan_max_depth")
    expected = {":root": "deny", ":minimal": "read", ":workspace_roots": {".": "read"}}
    return {
        "profile_shape": set(profile) == {"filesystem", "network"},
        "filesystem": filesystem == expected,
        "network_disabled": profile.get("network") == {"enabled": False},
        "no_extra_runtime_roots": not any(
            config.get(name)
            for name in (
                "runtime_workspace_roots",
                "additional_workspace_roots",
                "workspace_roots",
            )
        ),
    }


def representation_status(config):
    """Record only types/nullness and known booleans, never path/env values."""

    def shape(value):
        if not isinstance(value, dict):
            return {"type": type(value).__name__}
        return {
            digest(key): {"type": type(item).__name__, "null": item is None}
            for key, item in value.items()
        }

    profile = config.get("permissions", {}).get("clytie-review", {})
    network = profile.get("network", {})
    environment = config.get("shell_environment_policy", {}).get("set", {})
    return {
        "profile_shape": shape(profile),
        "network_shape": shape(network),
        "environment_shape": shape(environment),
        "network_enabled_flag_false": network.get("enabled") is False,
        "extends_absent_or_null": profile.get("extends") is None,
        "native_environment_entries": {
            name: {
                "present": name in environment,
                "type": type(environment.get(name)).__name__,
                "matches_requested_value": environment.get(name) == value,
            }
            for name, value in {
                "SystemRoot": "C:/Windows",
                "WINDIR": "C:/Windows",
                "SystemDrive": "C:",
            }.items()
        },
    }


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
        for name in sorted(names - ({"_default"} if category == "plugins" else set())):
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
    policy = {
        "mxc": config.get("windows", {}).get("sandbox") == "mxc",
        "profile": config.get("default_permissions") == "clytie-review",
        **permission_checks(config),
        "model": config.get("model") == MODEL,
        "effort": config.get("model_reasoning_effort") == EFFORT,
        "approval_never": config.get("approval_policy") == "never",
        "private_docs_disabled": config.get("project_doc_max_bytes") == 0,
        "tool_environment_none": config.get("shell_environment_policy", {}).get("inherit")
        == "none",
        "tool_environment_fixed": config.get("shell_environment_policy", {}).get("set")
        == {"SystemRoot": "C:/Windows", "WINDIR": "C:/Windows", "SystemDrive": "C:"},
        "no_environment_includes": not config.get("shell_environment_policy", {}).get(
            "include_only"
        ),
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
        for name, wanted in (
            ("allowedWindowsSandboxImplementations", "mxc"),
            ("allowedApprovalPolicies", "never"),
            ("allowedWebSearchModes", "disabled"),
        ):
            if req.get(name) is not None and wanted not in req[name]:
                blockers.append("managed_policy_conflict")
        if (
            req.get("allowedPermissionProfiles") is not None
            and req["allowedPermissionProfiles"].get("clytie-review") is not True
        ):
            blockers.append("managed_policy_conflict")
        if (
            any(req.get("featureRequirements", {}).get(name) is True for name in DISABLED_FEATURES)
            if req.get("featureRequirements")
            else False
        ):
            blockers.append("managed_policy_conflict")
    entrypoints = {
        "features_disabled": all(feature_checks.values()),
        "mcp_explicitly_disabled": all(r["explicitly_disabled"] for r in inventory["mcp_servers"]),
        "plugins_explicitly_disabled": all(r["explicitly_disabled"] for r in inventory["plugins"]),
        "plugin_default_disabled": config.get("plugins", {}).get("_default", {}).get("enabled")
        is False,
        "host_skills_skipped": features.get("skip_host_skill_discovery") is True,
        "bundled_skills_disabled": config.get("skills", {}).get("bundled", {}).get("enabled")
        is False,
        "skill_instructions_disabled": config.get("skills", {}).get("include_instructions")
        is False,
        "notify_disabled": config.get("notify") == [],
    }
    if not all(entrypoints.values()):
        blockers.append("plugin_entrypoint_inventory_not_verified")
    return {
        "ready_for_thread": not blockers,
        "blockers": sorted(set(blockers)),
        "policy": policy,
        "representation": representation_status(config),
        "features_disabled": feature_checks,
        "entrypoints": entrypoints,
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


def inspect_runtime(features, servers, apps, expected_mcp=()):
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
    extra_enabled = any(
        row.get("enabled") is True
        and row["name"] not in DISABLED_FEATURES
        and row["name"] != "skip_host_skill_discovery"
        and any(
            word in row["name"]
            for word in ("plugin", "app", "hook", "browser", "computer", "skill")
        )
        for row in features
    )
    inventory_complete = set(expected_mcp) <= {row["id_sha256"] for row in rows}
    passed = (
        all(feature_checks.values())
        and len(known) == len(features)
        and not extra_enabled
        and inventory_complete
        and isinstance(apps.get("apps"), list)
        and all(r["disabled"] and r["tools_empty"] and r["discovery_ok"] for r in rows)
        and all(r["disabled"] and r["uncallable"] for r in installed)
    )
    return {
        "passed": passed,
        "features": feature_checks,
        "mcp": rows,
        "apps": installed,
        "additional_entrypoint_enabled": extra_enabled,
        "inventory_complete": inventory_complete,
    }


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
                    event["error_mentions_mxc"] = "mxc" in str(message["error"]).lower()
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


def app_server_args(cli, overrides=()):
    args = [str(cli), "--no-daemon", "app-server", "--stdio", "--strict-config"]
    for value in [*SETTINGS, *EXTRA_SETTINGS, *overrides]:
        args.extend(["-c", value])
    return args


def initialize(server):
    server.rpc(
        "initialize",
        {
            "clientInfo": {"name": "clytie_review_preflight", "version": "1"},
            "capabilities": {"experimentalApi": True},
        },
    )
    server.send({"method": "initialized", "params": {}})


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
    args = app_server_args(cli)
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
        "snapshot_path_sha256": digest(str(snapshot)),
        "client_environment_names": sorted(client_environment(os.environ)),
        "ignore_user_config_supported": False,
        "raw_config_saved": False,
        "reusable_certificate": False,
    }
    server = None
    try:
        if version != metadata["cli_version"]:
            raise ReviewIncomplete("CLI version differs from candidate")
        if (
            metadata.get("cli_sha256") != evidence["cli"]["sha256"]
            or metadata.get("cli_path_sha256") != hashlib.sha256(str(cli).encode()).hexdigest()
        ):
            raise ReviewIncomplete("CLI pin differs from candidate")
        manifest = snapshot_manifest(snapshot)
        declared = json.loads((candidate / "snapshot-manifest.json").read_text(encoding="utf-8"))
        if declared != manifest or metadata.get("snapshot_manifest_sha256") != manifest["sha256"]:
            raise ReviewIncomplete("snapshot content mismatch")
        evidence["snapshot_content_sha256"] = manifest["sha256"]
        with (destination / "snapshot-manifest.json").open("x", encoding="utf-8") as output:
            json.dump(manifest, output, ensure_ascii=False, indent=2)
        # A discovery-only instance supplies identifiers, never an acceptance certificate.
        diagnostic = NoModelServer(args, snapshot)
        try:
            initialize(diagnostic)
            config = diagnostic.rpc("config/read", {"cwd": str(snapshot), "includeLayers": True})
            overrides = disable_catalogs(config)
            evidence["diagnostic"] = {
                "purpose": "catalog_identifiers_only",
                "override_count": sum(len(names) for names in catalog_names(config).values()),
                "override_argument_count": len(overrides),
                "no_thread": True,
            }
            del config
        finally:
            evidence["diagnostic_process"] = diagnostic.close()
            evidence["diagnostic_events"] = diagnostic.events
        if hashlib.sha256(cli.read_bytes()).hexdigest() != evidence["cli"]["sha256"]:
            raise ReviewIncomplete("CLI file changed")
        args = app_server_args(cli, overrides)
        evidence["args"] = args
        server = NoModelServer(args, snapshot)
        initialize(server)
        config = server.rpc("config/read", {"cwd": str(snapshot), "includeLayers": True})
        requirements = server.rpc("configRequirements/read")
        config_hash = digest([config, requirements])
        evidence["config"] = inspect_config(config, requirements)
        del config, requirements
        if not evidence["config"]["ready_for_thread"]:
            raise ReviewIncomplete("pre_thread_configuration_gate")
        result = server.rpc(
            "thread/start",
            {
                "cwd": str(snapshot),
                "ephemeral": True,
                "model": MODEL,
                "approvalPolicy": "never",
                "permissions": "clytie-review",
                "runtimeWorkspaceRoots": [str(snapshot)],
                "selectedCapabilityRoots": [],
                "allowProviderModelFallback": False,
                "config": {"model_reasoning_effort": EFFORT},
            },
        )
        thread = result["thread"]
        evidence["thread_created"] = True
        evidence["thread_id_sha256"] = digest(thread["id"])
        if (
            not thread.get("ephemeral")
            or result.get("model") != MODEL
            or result.get("reasoningEffort") != EFFORT
            or result.get("runtimeWorkspaceRoots") != [str(snapshot)]
            or result.get("activePermissionProfile", {}).get("id") != "clytie-review"
            or result.get("activePermissionProfile", {}).get("extends") is not None
        ):
            raise ReviewIncomplete("effective_thread_mismatch")
        features = all_pages(server.rpc, "experimentalFeature/list", thread["id"])
        servers = all_pages(server.rpc, "mcpServerStatus/list", thread["id"])
        apps = server.rpc("app/installed", {"threadId": thread["id"], "forceRefresh": False})
        evidence["runtime"] = inspect_runtime(
            features,
            servers,
            apps,
            [row["id_sha256"] for row in evidence["config"]["inventory"]["mcp_servers"]],
        )
        if not evidence["runtime"]["passed"]:
            raise ReviewIncomplete("thread_runtime_gate")
        current = server.rpc("config/read", {"cwd": str(snapshot), "includeLayers": True})
        current_requirements = server.rpc("configRequirements/read")
        evidence["configuration_unchanged"] = digest([current, current_requirements]) == config_hash
        del current, current_requirements
        if not evidence["configuration_unchanged"]:
            raise ReviewIncomplete("configuration changed during preflight")
        if snapshot_manifest(snapshot) != manifest:
            raise ReviewIncomplete("snapshot changed during preflight")
        raise ReviewIncomplete("thread_bound_tool_probe_interface_unverified")
    except ReviewIncomplete as exc:
        evidence["stopped_at"] = str(exc)
    except (OSError, ValueError, KeyError, TypeError):
        evidence["stopped_at"] = "invalid_or_missing_preflight_input"
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
