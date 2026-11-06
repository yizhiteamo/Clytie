"""Patch only enabled scalars; preserve every untouched byte of the YAML source."""

import re
from pathlib import Path
from uuid import uuid4

from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, ScalarNode
from ruamel.yaml.tokens import AliasToken, AnchorToken

from clytie.config import load_home
from clytie.plugin_host.snapshots import atomic_write, contained, identifier

from .auth import APIError


def patch_source(source, patch):
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

    def children(node):
        if not isinstance(node, MappingNode) or node.flow_style:
            raise APIError("config_not_writable")
        values = {}
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or key.value in values or key.value == "<<":
                raise APIError("config_not_writable")
            values[key.value] = value
        return values

    root = children(tree)
    newline = "\r\n" if "\r\n" in source else "\n"
    edits = []
    missing = []
    plugins = children(root["plugins"]) if "plugins" in root else {}
    for pid, fields in patch.get("plugins", {}).items():
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


class ConfigWriter:
    def __init__(self, home, clock, *, validate=None):
        self.home, self.clock = Path(home), clock
        self.validate = validate or (lambda: load_home(self.home).valid)

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
        identifier(cid)
        path = contained(self.home, self.home / "companions" / cid / "companion.yaml")
        before = path.read_bytes()
        after = patch_source(before.decode("utf-8"), patch).encode("utf-8")
        history = contained(self.home, path.parent / "config-history")
        history.mkdir(parents=True, exist_ok=True)
        suffix = self.clock.now().strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
        backup = history / ("companion.yaml." + suffix)
        atomic_write(backup, before)
        atomic_write(path, after)
        try:
            valid = self.validate()
        except Exception:
            valid = False
        if not valid:
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
