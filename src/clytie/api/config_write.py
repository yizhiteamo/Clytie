"""Edit companion YAML in place and retain configuration history."""

import json
import re
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from uuid import uuid4

import yaml as pyyaml
from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, ScalarNode, SequenceNode
from ruamel.yaml.tokens import AliasToken, AnchorToken

from clytie.config import load_home, repository_root
from clytie.plugin_host.manifests import ManifestError, read_manifest
from clytie.plugin_host.snapshots import atomic_write, contained, identifier

from .auth import APIError


def _document(source):
    yaml = YAML(typ="rt")
    try:
        if any(isinstance(t, (AliasToken, AnchorToken)) for t in yaml.scan(source)):
            raise APIError("config_not_writable")
        tree = yaml.compose(source)
    except APIError:
        raise
    except Exception:
        raise APIError("config_not_writable") from None
    if not isinstance(tree, MappingNode):
        raise APIError("config_not_writable")

    return tree


def children(node):
    if getattr(node, "flow_style", False):
        raise APIError("config_not_writable")
    return mapping(node)


def mapping(node):
    if not isinstance(node, MappingNode):
        raise APIError("config_not_writable")
    values = {}
    for key, value in node.value:
        if not isinstance(key, ScalarNode) or key.value in values or key.value == "<<":
            raise APIError("config_not_writable")
        values[key.value] = value
    return values


def patch_source(source, patch):
    root = children(_document(source))
    newline = "\r\n" if "\r\n" in source else "\n"
    edits = []
    missing = []
    plugins = children(root["plugins"]) if "plugins" in root else {}
    updates = patch.get("plugins", {})
    if not isinstance(updates, dict):
        raise APIError("config_not_writable")
    for pid, fields in updates.items():
        identifier(pid)
        if set(fields) != {"enabled"} or not isinstance(fields["enabled"], bool):
            raise APIError("config_not_writable")
        value = "true" if fields["enabled"] else "false"
        if pid not in plugins:
            missing.append(f"  {pid}:{newline}    enabled: {value}{newline}")
            continue
        fields_node = children(plugins[pid])
        if "enabled" in fields_node:
            node = fields_node["enabled"]
            if not isinstance(node, ScalarNode) or node.tag != "tag:yaml.org,2002:bool":
                raise APIError("config_not_writable")
            edits.append((node.start_mark.index, node.end_mark.index, value))
        else:
            node = plugins[pid]
            # Insert at the first child's line, preserving comments and all existing formatting.
            if not node.value:
                raise APIError("config_not_writable")
            pos = node.value[0][0].start_mark
            start = source.rfind("\n", 0, pos.index) + 1
            edits.append((start, start, " " * pos.column + "enabled: " + value + newline))
    if missing:
        if "plugins" not in root:
            edits.append(
                (
                    len(source),
                    len(source),
                    ("" if source.endswith("\n") else newline)
                    + "plugins:"
                    + newline
                    + "".join(missing),
                )
            )
        else:
            node = root["plugins"]
            # New entries only for the conventional indentation; refuse rather than reformat.
            if node.value and node.value[0][0].start_mark.column != 2:
                raise APIError("config_not_writable")
            pos = node.end_mark.index
            edits.append(
                (pos, pos, ("" if source[:pos].endswith("\n") else newline) + "".join(missing))
            )
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]
    return source


def proposal_source(source, pid, account, changes):
    """Apply a plugin's values to exactly one existing binding."""
    root = children(_document(source))
    channels = root.get("channels")
    if channels is not None and not isinstance(channels, SequenceNode):
        raise APIError("config_not_writable")
    matches = []
    for entry in channels.value if channels else ():
        plugin = mapping(entry).get("plugin")
        if isinstance(plugin, ScalarNode) and plugin.value == pid:
            matches.append(entry)
    if account is None:
        if matches or "plugins" not in root:
            raise APIError("config_binding_mismatch", 409)
        binding = mapping(root["plugins"]).get(pid)
    else:
        if len(matches) != 1:
            raise APIError("config_binding_mismatch", 409)
        binding = matches[0]
        account_node = mapping(binding).get("account")
        if not isinstance(account_node, ScalarNode) or account_node.value != account:
            raise APIError("config_binding_mismatch", 409)
    if not isinstance(binding, MappingNode):
        raise APIError("config_binding_mismatch", 409)

    newline = "\r\n" if "\r\n" in source else "\n"
    edits = []

    def encode(value):
        return json.dumps(value, ensure_ascii=False, allow_nan=False)

    def replace(child, key, value):
        start, end = child.start_mark.index, child.end_mark.index
        replacement = encode(value)
        if isinstance(child, ScalarNode) and child.style in {"|", ">"}:
            # Keep the terminating newline and indentation before the following key.
            end = len(source[:end].rstrip("\r\n "))
        elif isinstance(child, (SequenceNode, MappingNode)) and not child.flow_style:
            # A block's end mark is the following sibling, beyond its trailing comments.
            end = max(
                token.end_mark.index
                for token in YAML(typ="rt").scan(source)
                if start <= token.start_mark.index < token.end_mark.index <= end
            )
            if child.start_mark.column <= key.start_mark.column:
                replacement = " " + replacement
        edits.append((start, end, replacement))

    def update(node, values):
        fields = mapping(node)
        key_nodes = {key.value: key for key, _ in node.value}
        added = []
        for key, value in values.items():
            child = fields.get(key)
            if child is None:
                added.append(f"{encode(key)}: {encode(value)}")
            elif (
                isinstance(child, MappingNode)
                and isinstance(value, dict)
                and mapping(child).keys() <= value.keys()
            ):
                update(child, value)
            else:
                replace(child, key_nodes[key], value)
        if not added:
            return
        if node.flow_style:
            pos = node.start_mark.index + 1
            edits.append((pos, pos, ", ".join(added) + (", " if fields else "")))
            return
        first_key = node.value[0][0].start_mark
        start = source.rfind("\n", 0, first_key.index) + 1
        if source[start : first_key.index].strip():
            # In a compact sequence mapping insert after the dash, before its first key.
            text = (newline + " " * first_key.column).join(added + [""])
            edits.append((first_key.index, first_key.index, text))
        else:
            text = "".join(" " * first_key.column + line + newline for line in added)
            edits.append((start, start, text))

    config = mapping(binding).get("config")
    if config is None:
        update(binding, {"config": changes})
    else:
        update(config, changes)
    expected = pyyaml.safe_load(source)
    plugins = expected.get("plugins", {})
    if not isinstance(plugins, dict) or not isinstance(plugins.get(pid, {}), dict):
        raise APIError("config_not_writable")
    target = (
        plugins[pid]
        if account is None
        else next(entry for entry in expected["channels"] if entry["plugin"] == pid)
    )
    if plugins.get(pid, {}).get("enabled") is False:
        raise APIError("config_binding_mismatch", 409)
    target.setdefault("config", {}).update(changes)
    for start, end, text in sorted(edits, reverse=True):
        source = source[:start] + text + source[end:]
    try:
        if json.dumps(pyyaml.safe_load(source), sort_keys=True) != json.dumps(
            expected, sort_keys=True
        ):
            raise APIError("config_not_writable")
    except pyyaml.YAMLError:
        raise APIError("config_not_writable") from None
    return source


class ConfigWriter:
    def __init__(self, home, clock, *, validate=None):
        self.home, self.clock = Path(home), clock
        self._write_lock = Lock()
        self.validate = validate or (lambda: load_home(self.home).valid)

    @contextmanager
    def _commit(self):
        from clytie.backup.coordination import CaptureCoordinator, CaptureError

        path = contained(self.home, self.home / "supervisor/config-commit.lock")
        coordinator = CaptureCoordinator(path, self.clock)
        try:
            with coordinator.commit():
                yield
        except CaptureError:
            raise APIError("config_not_writable") from None

    def preview(self, cid, patch):
        identifier(cid)
        path = contained(self.home, self.home / "companions" / cid / "companion.yaml")
        try:
            return patch_source(path.read_bytes().decode("utf-8"), patch)
        except APIError as exc:
            exc.result = {
                "patch": patch,
                "description": "配置不能安全写回，请手动修改列出的插件 enabled 项",
            }
            raise

    def save(self, cid, patch):
        return self._save(cid, lambda source: patch_source(source, patch))

    def save_proposal(self, cid, pid, account, changes):
        identifier(pid)

        def prepare(source):
            root = repository_root()
            bases = [self.home / "plugins"] + ([root / "plugins"] if root else [])
            paths = [
                base / pid / "plugin.json"
                for base in bases
                if (base / pid / "plugin.json").is_file()
            ]
            if len(paths) != 1:
                raise APIError("plugin_unavailable", 409)
            try:
                manifest = read_manifest(paths[0], repo_root=root)
            except ManifestError:
                raise APIError("plugin_unavailable", 409) from None
            if not changes or changes.keys() - set(manifest.data.get("config_writable", ())):
                raise APIError("config_not_writable")
            candidate = proposal_source(source, pid, account, changes)
            report = load_home(self.home, _companion_source=(cid, candidate.encode("utf-8")))
            if not report.valid or cid not in report.plugin_configs:
                raise APIError("config_invalid")
            return candidate

        return self._save(cid, prepare)

    def _save(self, cid, prepare):
        with self._write_lock:
            return self._save_locked(cid, prepare)

    def _save_locked(self, cid, prepare):
        identifier(cid)
        path = contained(self.home, self.home / "companions" / cid / "companion.yaml")
        before = path.read_bytes()
        after = prepare(before.decode("utf-8")).encode("utf-8")
        history = contained(self.home, path.parent / "config-history")
        history.mkdir(parents=True, exist_ok=True)
        suffix = self.clock.now().strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
        backup = history / ("companion.yaml." + suffix)
        atomic_write(backup, before)
        with self._commit():
            if path.read_bytes() != before:
                raise APIError("config_not_writable")
            atomic_write(path, after)
        try:
            valid = self.validate()
        except Exception:
            valid = False
        if not valid:
            with self._commit():
                if path.read_bytes() == after:
                    atomic_write(path, before)
            raise APIError("config_invalid_after_write")
        files = sorted(
            p
            for p in history.iterdir()
            if re.fullmatch(r"companion\.yaml\.[0-9TZ]+-[a-f0-9]+", p.name)
        )
        for old in files[:-10]:
            contained(self.home, old)
            if old.is_symlink() or not old.is_file():
                raise APIError("config_not_writable")
            old.unlink()
        return {"saved": True}
