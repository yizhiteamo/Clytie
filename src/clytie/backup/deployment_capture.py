"""已声明部署文档与配置历史；历史值按自身 Schema 排除凭据。"""

import re
import stat

import yaml

from clytie.plugin_host.snapshots import contained

from .container import _name
from .credentials import document_exclusions, redact_document

_EXCLUDED_NAMES = {
    ".git",
    ".venv",
    "__pycache__",
    "secrets",
    "run",
    "backups",
    "exports",
    "private-denylist.txt",
}


def _identity(path):
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_mode, value.st_size, value.st_mtime_ns


def _tree(home, directory, check):
    """在进入每一目录前拒绝链接；记下目录和文件身份以发现捕获中变化。"""
    from .source import SourceError

    contained(home, directory)
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise SourceError("deployment_object_unsupported")
    result, pending = {}, [directory]
    while pending:
        check()
        path = pending.pop()
        if path != directory and path.name.casefold() in _EXCLUDED_NAMES:
            continue
        path = contained(home, path)
        identity = _identity(path)
        result[path.relative_to(home).as_posix()] = identity
        if stat.S_ISDIR(identity[2]):
            pending.extend(sorted(path.iterdir(), reverse=True))
        elif not stat.S_ISREG(identity[2]):
            raise SourceError("deployment_object_unsupported")
    return result


def capture_deployment(home, work, companions, repo_root, check):
    """只读固定命名空间；失败材料留在包根之外，不能以空捕获宣称完整。"""
    from .source import SourceError, _copy, _put, _small

    roots = [(home / "docs", None), (home / "config-history", "instance")]
    roots.extend((home / "companions" / cid / "config-history", "companion") for cid in companions)
    snapshots, entries, names = [], [], {}
    # The declared standalone deployment rules are documents, not the private
    # commit-scanning denylist. Only this explicit instance-relative file.
    rules = contained(home, home / "AGENTS.local.md")
    rules_identity = None
    if rules.exists():
        rules_identity = _identity(rules)
        if not stat.S_ISREG(rules_identity[2]):
            raise SourceError("deployment_object_unsupported")
        _name("AGENTS.local.md", names)
        entries.append(_copy(home, rules, work, "AGENTS.local.md", check))
    for directory, kind in roots:
        snapshot = _tree(home, directory, check)
        snapshots.append((directory, snapshot))
        for name, identity in snapshot.items():
            check()
            if stat.S_ISDIR(identity[2]):
                continue
            _name(name, names)
            path = contained(home, home / name)
            if kind is None:
                entries.append(_copy(home, path, work, name, check))
                continue
            filename = "clytie.local.yaml" if kind == "instance" else "companion.yaml"
            if path.suffix not in {".yaml", ".yml"} and not re.fullmatch(
                re.escape(filename) + r"\.[0-9TZ]+-[a-f0-9]+", path.name
            ):
                raise SourceError("opaque_configuration_history")
            raw = _small(path)
            if _identity(contained(home, path)) != identity:
                raise SourceError("snapshot_inconsistent")
            document = yaml.safe_load(raw)
            exclusions = document_exclusions(
                home, name, document, repo_root=repo_root, document_kind=kind
            )
            redacted = redact_document(document, name, exclusions)
            entries.append(_put(work, name, yaml.safe_dump(redacted, allow_unicode=True).encode()))
    for directory, before in snapshots:
        if _tree(home, directory, check) != before:
            raise SourceError("snapshot_inconsistent")
    if (rules.exists() and _identity(contained(home, rules)) or None) != rules_identity:
        raise SourceError("snapshot_inconsistent")
    return entries
