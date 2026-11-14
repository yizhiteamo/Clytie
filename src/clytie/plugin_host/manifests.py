"""发现插件并离线校验；读取清单时不导入或执行插件。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import TypeAdapter, ValidationError

from clytie.config import ConfigIssue, _Reader, freeze
from clytie.sdk import __version__ as SDK_VERSION
from clytie.sdk.behavior import AttentionDirective, BeforeAttentionDirective
from clytie.sdk.types import DeliveryIntent, ReplyDraft


class ManifestError(ValueError):
    def __init__(self, issues: list[ConfigIssue]):
        self.issues = tuple(issues)
        super().__init__("; ".join(f"{i.file}:{i.pointer}: {i.expected}" for i in issues))


@dataclass(frozen=True)
class Manifest:
    path: Path
    data: Mapping[str, Any]

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def version(self) -> Version:
        return Version(self.data["version"])

    @property
    def priority(self) -> int:
        return self.data.get("priority", 100)

    @property
    def backup_mode(self) -> str:
        return self.data.get("backup", {}).get("mode", "unsupported")


def read_manifest(path: Path, *, repo_root: Path | None = None) -> Manifest:
    reader = _Reader(repo_root)
    data = reader.document(path, "plugin")
    if reader.errors:
        raise ManifestError(reader.errors)
    if data is not None:
        if data.get("id") != path.parent.name:
            reader.issue(path, ("id",), f"directory id {path.parent.name}", data.get("id"))
        for field, value in [(("version",), data.get("version"))]:
            try:
                Version(value)
            except (InvalidVersion, TypeError):
                reader.issue(path, field, "valid plugin version", value)
        dependencies = data.get("dependencies", {})
        expressions = [(("requires", "sdk"), data.get("requires", {}).get("sdk"))]
        if isinstance(dependencies, dict):
            expressions.extend(
                (("dependencies", key, "version"), spec.get("version"))
                for key, spec in dependencies.items()
                if not key.startswith("x-") and isinstance(spec, dict)
            )
        for position, expression in expressions:
            if not isinstance(expression, str):
                continue
            try:
                versions = SpecifierSet(expression)
                if position == ("requires", "sdk") and Version(SDK_VERSION) not in versions:
                    reader.issue(path, position, f"range accepting SDK {SDK_VERSION}", expression)
            except InvalidSpecifier:
                reader.issue(path, position, "PEP 440 version range", expression)
        if isinstance(data.get("config"), dict):
            reader.plugin_schema(data["config"], path, ("config",))
        types = {
            "before_attention": BeforeAttentionDirective,
            "attention": AttentionDirective,
            "after_generation": ReplyDraft,
            "before_send": list[DeliveryIntent],
        }
        for phase, value in data.get("failure_default", {}).items():
            if phase.startswith("x-") or value is None:
                continue
            try:
                if phase not in types:
                    raise ValueError("observer defaults must be null")
                TypeAdapter(types[phase]).validate_python(value)
            except (ValidationError, ValueError):
                reader.issue(path, ("failure_default", phase), "typed SDK default or null", value)
        for field, key in (("api_routes", "path"), ("webui_panels", "data_route")):
            for declaration in data["contributes"].get(field, []):
                if not declaration[key].startswith(f"/plugins/{data['id']}/"):
                    reader.issue(
                        path,
                        ("contributes", field),
                        "route within plugin namespace",
                        declaration[key],
                    )
    if reader.errors:
        raise ManifestError(reader.errors)
    if data is None:
        raise ManifestError([ConfigIssue(str(path), "", "plugin manifest object", None)])
    return Manifest(path.resolve(), freeze(data))


def discover(*directories: Path, repo_root: Path | None = None) -> dict[str, Manifest]:
    result: dict[str, Manifest] = {}
    issues: list[ConfigIssue] = []
    for directory in directories:
        if not directory.is_dir():
            continue
        for child in sorted(directory.iterdir()):
            if not child.is_dir() or child.is_symlink():
                continue
            path = child / "plugin.json"
            if not path.is_file():
                continue
            try:
                manifest = read_manifest(path, repo_root=repo_root)
            except ManifestError as exc:
                issues.extend(exc.issues)
                continue
            if manifest.id in result:
                issues.append(
                    ConfigIssue(str(path), "/id", "globally unique plugin id", manifest.id)
                )
            else:
                result[manifest.id] = manifest
    if issues:
        raise ManifestError(issues)
    return result
