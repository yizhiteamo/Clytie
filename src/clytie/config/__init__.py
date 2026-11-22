"""Read-only instance configuration loading and validation."""

from __future__ import annotations

import copy
import json
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from importlib.resources import files
from pathlib import Path, PureWindowsPath
from string import Formatter
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from jsonschema import Draft202012Validator, SchemaError
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from referencing import Registry
from referencing.exceptions import NoSuchResource, Unresolvable

from .assets import AssetDeclarationError, resolved_dependencies, validate_asset_schema


def compile_input_pattern(pattern):
    """Preserve regex escapes; match normalized input case-insensitively."""
    return re.compile(pattern, re.IGNORECASE)


def safety_belt_settings(config):
    values = config.get("boundary", {}).get("safety_belt", {})
    return {
        key: values.get(key, default)
        for key, default in (("replies", 200), ("window_minutes", 30), ("pause_minutes", 30))
    }


_MISSING = object()


def freeze(value: Any) -> Any:
    """Recursively detach and freeze JSON-shaped configuration values."""
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


def thaw(value: Any) -> Any:
    """Return a JSON-serializable, detached copy of a frozen value."""
    if isinstance(value, Mapping):
        return {key: thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def deep_merge(*layers: Mapping[str, Any]) -> Mapping[str, Any]:
    """Merge objects recursively; replace arrays/scalars, including null values."""
    result: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
                result[key] = thaw(deep_merge(result[key], value))
            else:
                result[key] = thaw(value)
    return freeze(result)


def _pointer(parts: Any) -> str:
    return "".join("/" + str(part).replace("~", "~0").replace("/", "~1") for part in parts)


def _entries(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if not key.startswith("x-")}


@dataclass(frozen=True)
class ConfigIssue:
    file: str
    pointer: str
    expected: str
    actual: Any
    code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "actual", freeze(self.actual))

    def to_dict(self) -> dict[str, Any]:
        return {
            "file": self.file,
            "pointer": self.pointer,
            "expected": self.expected,
            "actual": thaw(self.actual),
            **({"code": self.code} if self.code is not None else {}),
        }


@dataclass(frozen=True)
class HomeReport:
    home: str
    exists: bool
    companions: tuple[Mapping[str, Any], ...] = ()
    errors: tuple[ConfigIssue, ...] = ()
    instance: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    plugin_configs: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    failed_plugin_state: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    ledger_errors: tuple[Mapping[str, Any], ...] = ()
    ledger_notices: tuple[Mapping[str, Any], ...] = ()
    dialogue_notices: tuple[Mapping[str, Any], ...] = ()

    global_errors: tuple[ConfigIssue, ...] = ()

    @property
    def valid(self) -> bool:
        return self.exists and not self.errors

    @property
    def exit_code(self) -> int:
        return 2 if not self.exists else int(not self.valid or bool(self.ledger_errors))

    def to_dict(self) -> dict[str, Any]:
        return {
            "ledger_errors": thaw(self.ledger_errors),
            "ledger_notices": thaw(self.ledger_notices),
            "dialogue_notices": thaw(self.dialogue_notices),
            "home": self.home,
            "exists": self.exists,
            "valid": self.valid,
            "companions": thaw(self.companions),
            "errors": [error.to_dict() for error in self.errors],
            "global_errors": [error.to_dict() for error in self.global_errors],
            "failed_plugin_state": thaw(self.failed_plugin_state),
        }


class _UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader: _UniqueLoader, node: yaml.MappingNode) -> dict[str, Any]:
    loader.flatten_mapping(node)
    result: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str):
            raise ValueError("mapping keys must be strings")
        if key in result:
            raise ValueError(f"duplicate mapping key: {key}")
        result[key] = loader.construct_object(value_node)
    return result


_UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _json_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate mapping key: {key}")
        result[key] = value
    return result


def _no_remote_resource(uri: str) -> Any:
    raise NoSuchResource(ref=uri)


def repository_root() -> Path | None:
    """Locate a checkout by its files, without importing repository code."""
    source = Path(__file__).resolve().parents[3]
    working = Path.cwd().resolve()
    for candidate in (source, working, *working.parents):
        if (
            (candidate / "pyproject.toml").is_file()
            and (candidate / "schemas" / "instance.schema.json").is_file()
            and (candidate / "src" / "clytie").is_dir()
        ):
            return candidate
    return None


@lru_cache(maxsize=1)
def default_consolidator() -> str:
    schema = files("clytie").joinpath("schemas", "companion.schema.json")
    if not schema.is_file():
        root = repository_root()
        if root is None or not (root / "schemas" / "companion.schema.json").is_file():
            raise FileNotFoundError("default consolidator requires companion.schema.json")
        schema = root / "schemas" / "companion.schema.json"
    properties = json.loads(schema.read_text(encoding="utf-8"))["properties"]
    return properties["memory"]["properties"]["recollect"]["properties"]["consolidator"]["default"]


def resolve_home(home: str | Path | None = None, *, repo_root: Path | None = None) -> Path:
    chosen = home if home is not None else os.environ.get("CLYTIE_HOME")
    if chosen:
        return Path(chosen).expanduser().resolve()
    root = repo_root or repository_root()
    if root is None:
        raise ValueError("provide --home or CLYTIE_HOME when no Clytie checkout is available")
    return root.parent / "Clytie-home"


class _Reader:
    def __init__(self, root: Path | None) -> None:
        self.root = root
        self.errors: list[ConfigIssue] = []
        self.schemas: dict[str, dict[str, Any]] = {}
        self.source_overrides: dict[Path, bytes] = {}

    def issue(
        self, file: Path, parts: Any, expected: str, actual: Any, *, code: str | None = None
    ) -> None:
        if file.name == "clytie.local.yaml" and parts and parts[0] == "credentials":
            actual = "invalid credential declaration" if code is None else actual
        self.errors.append(ConfigIssue(str(file), _pointer(parts), expected, actual, code))

    def read(self, file: Path) -> Any:
        try:
            if file.is_symlink() or "secrets" in {part.casefold() for part in file.resolve().parts}:
                self.issue(
                    file,
                    (),
                    "configuration file outside secrets without symlink",
                    "restricted path",
                )
                return _MISSING
            text = (
                self.source_overrides[file].decode("utf-8-sig")
                if file in self.source_overrides
                else file.read_text(encoding="utf-8-sig")
            )
            value = (
                json.loads(text, object_pairs_hook=_json_pairs)
                if file.suffix == ".json"
                else yaml.load(text, Loader=_UniqueLoader)
            )
            # YAML dates, non-finite numbers and aliases must not escape the JSON contract.
            json.dumps(value, allow_nan=False)
            return value
        except (
            OSError,
            UnicodeError,
            yaml.YAMLError,
            ValueError,
            TypeError,
            RecursionError,
        ) as exc:
            self.issue(
                file, (), "readable UTF-8 JSON-compatible configuration with unique keys", str(exc)
            )
            return _MISSING

    def validate(
        self,
        data: Any,
        schema: dict[str, Any],
        file: Path,
        prefix: tuple[Any, ...] = (),
        origins: dict[str, tuple[Path, tuple[Any, ...]]] | None = None,
        *,
        global_origins: list[tuple[Path, tuple[Any, ...]]] | None = None,
        global_errors: list[ConfigIssue] | None = None,
    ) -> None:
        def emit(
            parts: tuple[Any, ...], expected: str, actual: Any, *, code: str | None = None
        ) -> None:
            target, location = file, prefix + parts
            selected_origin = None
            if origins:
                for length in range(len(parts), -1, -1):
                    origin = origins.get(_pointer(parts[:length]))
                    if origin:
                        selected_origin = origin
                        target, base = origin
                        location = base + parts[length:]
                        break
            self.issue(target, location, expected, actual, code=code)
            if global_errors is not None and any(
                selected_origin is origin for origin in (global_origins or ())
            ):
                global_errors.append(self.errors[-1])

        try:
            validator = Draft202012Validator(
                schema, registry=Registry(retrieve=_no_remote_resource)
            )
            errors = sorted(
                validator.iter_errors(data), key=lambda error: repr(list(error.absolute_path))
            )
            if not errors:
                try:
                    resolved_dependencies(schema, data)
                except AssetDeclarationError as exc:
                    emit(exc.parts, exc.code, "invalid declaration", code=exc.code)
            for error in errors:
                parts = tuple(error.absolute_path)
                if error.validator == "required" and isinstance(error.instance, dict):
                    for name in error.validator_value:
                        if name not in error.instance:
                            emit(
                                parts + (name,), "required field", "missing", code="schema_required"
                            )
                    continue
                if (
                    error.validator == "additionalProperties"
                    and error.validator_value is False
                    and isinstance(error.instance, dict)
                ):
                    properties = error.schema.get("properties", {})
                    patterns = error.schema.get("patternProperties", {})
                    for name, value in error.instance.items():
                        if name not in properties and not any(
                            re.search(pattern, name) for pattern in patterns
                        ):
                            emit(parts + (name,), "declared field or x- extension", value)
                    continue
                emit(
                    parts,
                    f"{error.validator}: {json.dumps(error.validator_value, ensure_ascii=False)}",
                    error.instance,
                )
        except (SchemaError, Unresolvable, TypeError, ValueError, RecursionError) as exc:
            self.issue(file, prefix, "valid draft 2020-12 schema and JSON data", str(exc))

    def document(self, file: Path, kind: str) -> Any:
        value = self.read(file)
        if value is _MISSING:
            return None
        if kind not in self.schemas:
            bundled = files("clytie").joinpath("schemas", f"{kind}.schema.json")
            if bundled.is_file():
                try:
                    schema = json.loads(bundled.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, ValueError) as exc:
                    self.issue(Path(str(bundled)), (), "readable bundled JSON Schema", str(exc))
                    return None
            else:
                if self.root is None:
                    self.issue(file, (), "bundled configuration schemas", "missing")
                    return None
                schema_file = self.root / "schemas" / f"{kind}.schema.json"
                schema = self.read(schema_file)
            if not isinstance(schema, dict):
                return None
            self.schemas[kind] = schema
        self.validate(value, self.schemas[kind], file)
        return value if isinstance(value, dict) else None

    def plugin_schema(self, schema: Any, file: Path, parts: tuple[Any, ...]) -> bool:
        start = len(self.errors)
        try:
            Draft202012Validator.check_schema(schema)
        except (SchemaError, TypeError) as exc:
            self.issue(file, parts, "valid draft 2020-12 JSON Schema", str(exc))
            return False

        def references(node: Any, position: tuple[Any, ...]) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if (
                        key in {"$ref", "$dynamicRef"}
                        and isinstance(value, str)
                        and not value.startswith("#")
                    ):
                        self.issue(
                            file,
                            position + (key,),
                            "local fragment reference for offline validation",
                            value,
                        )
                    references(value, position + (key,))
            elif isinstance(node, list):
                for index, value in enumerate(node):
                    references(value, position + (index,))

        references(schema, parts)
        try:
            validate_asset_schema(schema)
        except (AssetDeclarationError, SchemaError, TypeError, ValueError) as exc:
            position = getattr(exc, "parts", ())
            self.issue(file, parts + position, str(exc), "invalid asset declaration")
        return len(self.errors) == start


def _default_config(
    schema: dict[str, Any], file: Path
) -> tuple[dict[str, Any], dict[str, tuple[Path, tuple[Any, ...]]]]:
    origins: dict[str, tuple[Path, tuple[Any, ...]]] = {}

    def mark(value: Any, source: tuple[Any, ...], target: tuple[Any, ...]) -> None:
        key = _pointer(target)
        if not isinstance(value, dict):
            for previous in tuple(origins):
                if previous == key or previous.startswith(key + "/"):
                    origins.pop(previous)
        origins[key] = (file, source)
        if isinstance(value, dict):
            for name, child in value.items():
                mark(child, source + (name,), target + (name,))

    def collect(node: Any, schema_path: tuple[Any, ...], value_path: tuple[Any, ...]) -> Any:
        if not isinstance(node, dict):
            return _MISSING
        value = _MISSING
        properties = node.get("properties", {})
        if isinstance(properties, dict):
            for key, child in properties.items():
                default = collect(child, schema_path + ("properties", key), value_path + (key,))
                if default is not _MISSING:
                    if value is _MISSING:
                        value = {}
                    if isinstance(value, dict):
                        value[key] = default
        if "default" in node:
            own_default = copy.deepcopy(node["default"])
            value = (
                thaw(deep_merge(value, own_default))
                if isinstance(value, dict) and isinstance(own_default, dict)
                else own_default
            )
            mark(own_default, schema_path + ("default",), value_path)
        return value

    defaults = collect(schema, ("config",), ())
    return (defaults if isinstance(defaults, dict) else {}), origins


def _merge_origins(
    layers: list[tuple[Mapping[str, Any], Path, tuple[Any, ...]]],
    origins: dict[str, tuple[Path, tuple[Any, ...]]],
) -> None:
    def visit(value: Any, file: Path, source: tuple[Any, ...], target: tuple[Any, ...]) -> None:
        # Parent origins cover new keys and missing required children after replacements.
        key = _pointer(target)
        if not isinstance(value, Mapping):
            for previous in tuple(origins):
                if previous == key or previous.startswith(key + "/"):
                    origins.pop(previous)
        origins[key] = (file, source)
        if isinstance(value, Mapping):
            for name, child in value.items():
                visit(child, file, source + (name,), target + (name,))

    for layer, file, prefix in layers:
        visit(layer, file, prefix, ())


def _safe_child(parent: Path, relative: Any) -> Path | None:
    if not isinstance(relative, str) or not relative:
        return None
    supplied = Path(relative)
    if supplied.is_absolute():
        return None
    try:
        candidate = (parent / supplied).resolve()
        allowed = candidate.is_relative_to(parent.resolve()) and "secrets" not in {
            part.casefold() for part in candidate.parts
        }
        return candidate if allowed else None
    except (OSError, RuntimeError, ValueError):
        return None


def credential_reference_state(directory: Path, relative: Any) -> str:
    """Check file metadata only, rejecting links in every path component."""
    if not isinstance(relative, str) or not relative or PureWindowsPath(relative).drive:
        return "invalid"
    supplied = Path(relative.replace("\\", "/"))
    if supplied.is_absolute() or ".." in supplied.parts:
        return "invalid"
    secret_root = directory / "secrets"
    candidate = secret_root / supplied
    try:
        if not candidate.resolve().is_relative_to(secret_root.resolve()):
            return "invalid"
        for component in (candidate, *candidate.parents):
            if component.is_symlink() or (
                component.exists()
                and getattr(component.lstat(), "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                return "invalid"
            if component == directory:
                break
        if candidate.is_file():
            return "found"
        return "invalid" if candidate.exists() else "missing"
    except (OSError, RuntimeError, ValueError):
        return "invalid"


def load_home(
    home: str | Path | None = None,
    *,
    repo_root: Path | None = None,
    _instance_source: bytes | None = None,
    _companion_source: tuple[str, bytes] | None = None,
) -> HomeReport:
    """Inspect files without importing plugins, opening databases or using the network."""
    root = repo_root.resolve() if repo_root is not None else repository_root()
    reader = _Reader(root)
    try:
        directory = resolve_home(home, repo_root=root)
    except (OSError, RuntimeError, ValueError) as exc:
        reader.issue(Path.cwd(), (), "explicit --home or CLYTIE_HOME outside a checkout", str(exc))
        return HomeReport("", False, errors=tuple(reader.errors))
    if not directory.is_dir():
        reader.issue(directory, (), "existing instance directory", "missing")
        return HomeReport(str(directory), False, errors=tuple(reader.errors))
    instance_file = directory / "clytie.local.yaml"
    if _instance_source is not None:
        reader.source_overrides[instance_file] = _instance_source
    instance = reader.document(instance_file, "instance")
    if instance is None:
        return HomeReport(str(directory), True, errors=tuple(reader.errors))
    defaults, _ = _default_config(reader.schemas["instance"]["properties"]["limits"], instance_file)
    if isinstance(instance.get("limits", {}), dict):
        instance["limits"] = thaw(deep_merge(defaults, instance.get("limits", {})))
    for key in ("supervisor", "notify", "backup"):
        defaults, _ = _default_config(reader.schemas["instance"]["properties"][key], instance_file)
        if isinstance(instance.get(key, {}), dict):
            instance[key] = thaw(deep_merge(defaults, instance.get(key, {})))
    backup = instance.get("backup", {})
    if isinstance(backup, dict):
        for group, count, minimum in (
            ("retention", "recent_memory", "minimum_per_group"),
            ("mirror", "recent", "minimum"),
        ):
            values = backup.get(group, {})
            if isinstance(values, dict):
                recent, floor = values.get(count), values.get(minimum)
                if type(recent) is int and type(floor) is int and recent < floor:
                    reader.issue(
                        instance_file,
                        ("backup", group, count),
                        f"at least {minimum}",
                        recent,
                    )
        mirror = backup.get("mirror", {})
        if isinstance(mirror, dict) and mirror.get("enabled") is True:
            if not isinstance(mirror.get("path"), str) or not mirror["path"].strip():
                reader.issue(
                    instance_file,
                    ("backup", "mirror", "path"),
                    "explicit mirror directory when enabled",
                    mirror.get("path"),
                )
    supervisor = instance.get("supervisor", {})
    if isinstance(supervisor, dict):
        heartbeat = supervisor.get("heartbeat_seconds", 5)
        hang = supervisor.get("hang_seconds", 30)
        if isinstance(heartbeat, int) and isinstance(hang, int) and hang <= 2 * heartbeat:
            reader.issue(
                instance_file,
                ("supervisor", "hang_seconds"),
                "greater than twice heartbeat_seconds",
                hang,
            )
    try:
        timezone = instance.get("timezone")
        if isinstance(timezone, str):
            ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        reader.issue(instance_file, ("timezone",), "IANA timezone", timezone)
    people = _entries(instance.get("people"))
    accounts = _entries(instance.get("accounts"))
    account_users: dict[str, str] = {}
    credentials = instance.get("credentials", {})
    for group, references in (("accounts", accounts), ("credentials", credentials)):
        for reference, value in references.items() if isinstance(references, dict) else ():
            if not isinstance(value, dict) or "secret" not in value:
                continue
            state = credential_reference_state(directory, value["secret"])
            if state != "found":
                reader.issue(
                    instance_file,
                    (group, reference, "secret"),
                    "existing file relative to secrets",
                    state if group == "credentials" else value["secret"],
                    code=f"credential_reference_{state}",
                )
    owners = [
        key
        for key, value in people.items()
        if isinstance(value, dict) and value.get("role") == "owner"
    ]
    if len(owners) != 1:
        reader.issue(instance_file, ("people",), "exactly one owner", owners)
    scenes = instance.get("scenes") if isinstance(instance.get("scenes"), list) else []
    conversations = _entries(instance.get("conversations"))
    conversation_identities = set()
    for name, conversation in conversations.items():
        if not isinstance(conversation, dict):
            continue
        identity = tuple(conversation.get(k) for k in ("channel", "kind", "platform_id"))
        if all(isinstance(v, str) for v in identity):
            if identity in conversation_identities:
                reader.issue(
                    instance_file,
                    ("conversations", name),
                    "unique channel/kind/platform_id",
                    "duplicate conversation identity",
                )
            conversation_identities.add(identity)
        if conversation.get("scene") not in scenes:
            reader.issue(
                instance_file,
                ("conversations", name, "scene"),
                "declared scene",
                conversation.get("scene"),
            )
        for index, member in enumerate(
            conversation.get("members_expected", [])
            if isinstance(conversation.get("members_expected", []), list)
            else []
        ):
            declared_companions = (
                instance.get("companions") if isinstance(instance.get("companions"), list) else []
            )
            if not isinstance(member, str) or (
                member not in people and member not in declared_companions
            ):
                reader.issue(
                    instance_file,
                    ("conversations", name, "members_expected", index),
                    "declared actor or companion id",
                    member,
                )
    if "test_conversation" in instance and (
        not isinstance(instance["test_conversation"], str)
        or instance["test_conversation"] not in conversations
    ):
        reader.issue(
            instance_file,
            ("test_conversation",),
            "declared conversation id",
            instance["test_conversation"],
        )
    instance_errors = len(reader.errors)
    global_errors = list(reader.errors)
    manifest_users = {}
    manifest_errors = {}
    current_companion = None
    manifests: dict[str, tuple[dict[str, Any] | None, Path, str]] = {}

    def manifest(
        plugin_id: Any, source: Path, position: tuple[Any, ...]
    ) -> tuple[dict[str, Any] | None, Path, str]:
        if (
            not isinstance(plugin_id, str)
            or not plugin_id
            or "/" in plugin_id
            or "\\" in plugin_id
            or plugin_id in {".", ".."}
        ):
            reader.issue(source, position, "plugin id without path separators", plugin_id)
            return None, source, "invalid"
        manifest_users.setdefault(plugin_id, set()).add(current_companion)
        if plugin_id in manifests:
            return manifests[plugin_id]
        manifest_start = len(reader.errors)
        bases = ([root / "plugins"] if root is not None else []) + [directory / "plugins"]
        candidates = [base / plugin_id / "plugin.json" for base in bases]
        available = [path for path in candidates if path.is_file()]
        if len(available) != 1:
            reader.issue(
                source,
                position,
                "exactly one plugin manifest in repository or instance",
                [str(path) for path in available],
            )
            result = (None, candidates[0], "missing" if not available else "invalid")
        else:
            file = available[0]
            start = len(reader.errors)
            data = reader.document(file, "plugin")
            if data:
                if data.get("id") != plugin_id:
                    reader.issue(file, ("id",), f"directory id {plugin_id}", data.get("id"))
                ranges = [
                    (
                        data.get("requires", {}).get("sdk")
                        if isinstance(data.get("requires"), dict)
                        else None,
                        ("requires", "sdk"),
                    )
                ]
                ranges += [
                    (dependency.get("version"), ("dependencies", key, "version"))
                    for key, dependency in _entries(data.get("dependencies")).items()
                    if isinstance(dependency, dict)
                ]
                for expression, pointer in ranges:
                    if isinstance(expression, str):
                        try:
                            SpecifierSet(expression)
                        except InvalidSpecifier:
                            reader.issue(file, pointer, "PEP 440 version range", expression)
                if isinstance(data.get("config"), dict):
                    reader.plugin_schema(data["config"], file, ("config",))
                    if "default" in data["config"] and not isinstance(
                        data["config"]["default"], dict
                    ):
                        reader.issue(
                            file,
                            ("config", "default"),
                            "object default for plugin configuration",
                            data["config"]["default"],
                        )
            result = (data, file, "found" if data and len(reader.errors) == start else "invalid")
        manifest_errors[plugin_id] = tuple(reader.errors[manifest_start:])
        manifests[plugin_id] = result
        return result

    for reference, value in credentials.items() if isinstance(credentials, dict) else ():
        allowed = value.get("plugins", []) if isinstance(value, dict) else []
        for index, pid in enumerate(allowed if isinstance(allowed, list) else []):
            manifest(pid, instance_file, ("credentials", reference, "plugins", index))
    instance_errors = len(reader.errors)
    global_errors = list(reader.errors)

    statuses: list[Mapping[str, Any]] = []
    dialogue_notices = []
    configurations: dict[str, Any] = {}
    companion_ids = (
        instance.get("companions") if isinstance(instance.get("companions"), list) else []
    )
    for companion_id in companion_ids:
        current_companion = companion_id
        start = len(reader.errors)
        companion_dir = _safe_child(directory / "companions", companion_id)
        if (
            companion_dir is None
            or not isinstance(companion_id, str)
            or Path(companion_id).name != companion_id
        ):
            reader.issue(
                instance_file, ("companions",), "companion id naming one directory", companion_id
            )
            continue
        file = companion_dir / "companion.yaml"
        if _companion_source is not None and _companion_source[0] == companion_id:
            reader.source_overrides[file] = _companion_source[1]
        data = reader.document(file, "companion")
        status: dict[str, Any] = {
            "id": companion_id,
            "valid": False,
            "model": None,
            "channels": [],
            "plugins": [],
            "persona": [],
        }
        if data is None:
            statuses.append(freeze(status))
            continue
        if data.get("id") != companion_id:
            reader.issue(file, ("id",), f"directory id {companion_id}", data.get("id"))
        for section in (
            "names",
            "agent",
            "replies",
            "outbound",
            "protection",
            "boundary",
            "context",
            "memory",
            "delivery",
            "tasks",
            "ledger",
            "task_reactions",
            "task_card",
        ):
            defaults, _ = _default_config(reader.schemas["companion"]["properties"][section], file)
            if isinstance(data.get(section, {}), dict):
                data[section] = thaw(deep_merge(defaults, data.get(section, {})))
        tasks = data.get("tasks", {})
        limits = instance.get("limits", {})
        deadline = tasks.get("default_deadline_seconds") if isinstance(tasks, dict) else None
        ceiling = limits.get("max_task_seconds") if isinstance(limits, dict) else None
        if (
            isinstance(deadline, (int, float))
            and isinstance(ceiling, (int, float))
            and deadline > ceiling
        ):
            reader.issue(
                file,
                ("tasks", "default_deadline_seconds"),
                "at most instance limits.max_task_seconds",
                deadline,
            )
        memory = data.get("memory", {})
        private_scenes = memory.get("private_scenes", []) if isinstance(memory, dict) else []
        names = data.get("names") if isinstance(data.get("names"), dict) else {}
        internal_scenes = names.get("internal_scenes", [])
        scene_fields = [
            ("memory", "private_scenes", private_scenes),
            ("names", "internal_scenes", internal_scenes),
        ]
        positions = [
            (section, key, index, scene)
            for section, key, values in scene_fields
            if isinstance(values, list)
            for index, scene in enumerate(values)
        ]
        for section, key, index, scene in positions:
            position = (section, key, index)
            if (
                section == "memory"
                and isinstance(internal_scenes, list)
                and scene not in internal_scenes
            ):
                reader.issue(file, position, "subset of names.internal_scenes", scene)
            if (
                not isinstance(scene, str)
                or scene not in scenes
                or scene in {"public_group", "stranger_private"}
            ):
                reader.issue(file, position, "declared private-capable scene", scene)
                continue
            if scene == "owner_private":
                continue
            trusted = set(owners) | set(companion_ids)
            for conversation in conversations.values():
                if not isinstance(conversation, dict) or conversation.get("scene") != scene:
                    continue
                members = conversation.get("members_expected")
                if not isinstance(members, list) or any(
                    member not in trusted for member in members
                ):
                    reader.issue(
                        file,
                        position,
                        "every conversation declares members_expected containing only owner/companions",
                        scene,
                    )
        protection = data.get("protection", {})
        if isinstance(protection, dict):
            enter, leave = protection.get("enter_ratio"), protection.get("exit_ratio")
            if (
                isinstance(enter, (int, float))
                and isinstance(leave, (int, float))
                and leave <= enter
            ):
                reader.issue(file, ("protection", "exit_ratio"), "greater than enter_ratio", leave)
        boundary = data.get("boundary", {})
        if isinstance(boundary, dict):
            if boundary.get("admission") == "test_only" and not instance.get("test_conversation"):
                reader.issue(
                    file,
                    ("boundary", "admission"),
                    "test_only requires test_conversation",
                    "test_only",
                )
        filters = boundary.get("input_filter", {}) if isinstance(boundary, dict) else {}
        patterns = filters.get("patterns", []) if isinstance(filters, dict) else []
        for index, pattern in enumerate(patterns if isinstance(patterns, list) else []):
            if isinstance(pattern, str):
                try:
                    compile_input_pattern(pattern)
                except re.error:
                    reader.issue(
                        file,
                        ("boundary", "input_filter", "patterns", index),
                        "compilable regular expression",
                        pattern,
                    )
        if "forbidden_names" not in data and isinstance(names.get("internal"), str):
            data["forbidden_names"] = [names["internal"]]
        for scene in _entries(names.get("external")):
            if scene not in scenes:
                reader.issue(file, ("names", "external", scene), "declared scene", scene)
        persona = data.get("persona") if isinstance(data.get("persona"), dict) else {}
        layers = _entries(persona.get("files"))
        mappings = _entries(persona.get("scenes"))
        for scene in scenes:
            if isinstance(scene, str) and scene not in mappings:
                reader.issue(
                    file,
                    ("persona", "scenes", scene),
                    "mapping for every declared scene",
                    "missing",
                )
        for scene, selected in mappings.items():
            if scene not in scenes:
                reader.issue(file, ("persona", "scenes", scene), "declared scene", scene)
            for index, layer in enumerate(selected if isinstance(selected, list) else []):
                if not isinstance(layer, str) or layer not in layers:
                    reader.issue(
                        file, ("persona", "scenes", scene, index), "declared persona layer", layer
                    )
        for layer, relative in layers.items():
            persona_file = _safe_child(companion_dir / "persona", relative)
            exists = persona_file is not None and persona_file.is_file()
            status["persona"].append(
                {"layer": layer, "path": relative, "status": "found" if exists else "missing"}
            )
            if not exists:
                reader.issue(
                    file,
                    ("persona", "files", layer),
                    "existing file inside persona directory",
                    relative,
                )
        for role, tools in _entries(data.get("permissions")).items():
            if role != "owner" and isinstance(tools, list) and "*" in tools:
                reader.issue(file, ("permissions", role), "wildcard allowed only for owner", tools)
        for tool, scope in _entries(data.get("tool_scopes")).items():
            excluded = scope.get("exclude", []) if isinstance(scope, dict) else []
            for index, actor_id in enumerate(excluded if isinstance(excluded, list) else []):
                person = (
                    instance.get("people", {}).get(actor_id) if isinstance(actor_id, str) else None
                )
                if not isinstance(person, dict) or person.get("role") == "owner":
                    reader.issue(
                        file,
                        ("tool_scopes", tool, "exclude", index),
                        "registered non-owner person",
                        actor_id,
                    )
        for scene, template in _entries(data.get("task_card")).items():
            if isinstance(template, str):
                try:
                    fields = [
                        name for _, name, _, _ in Formatter().parse(template) if name is not None
                    ]
                    if any(
                        name not in {"label", "id", "name", "helper", "duration"} for name in fields
                    ):
                        reader.issue(
                            file,
                            ("task_card", scene),
                            "template fields label/id/name/helper/duration",
                            fields,
                        )
                except ValueError:
                    reader.issue(file, ("task_card", scene), "valid task card template", template)
        references: list[tuple[Any, tuple[Any, ...], Any, str]] = []
        model = data.get("model")
        if isinstance(model, dict):
            references.append((model.get("plugin"), ("model",), model.get("config"), "model"))
        recollect_model = (
            memory.get("recollect", {}).get("model") if isinstance(memory, dict) else None
        )
        if isinstance(recollect_model, dict):
            references.append(
                (
                    recollect_model.get("plugin"),
                    ("memory", "recollect", "model"),
                    recollect_model.get("config"),
                    "recollect_model",
                )
            )
        channels = data.get("channels")
        if isinstance(channels, list):
            for index, channel in enumerate(channels):
                if not isinstance(channel, dict):
                    continue
                ref = channel.get("account")
                account = accounts.get(ref) if isinstance(ref, str) else None
                position = ("channels", index, "account")
                if not isinstance(account, dict):
                    reader.issue(file, position, "declared instance account reference", ref)
                else:
                    if account.get("channel") != channel.get("plugin"):
                        reader.issue(file, position, "account channel matches channel plugin", ref)
                    if ref in account_users and account_users[ref] != companion_id:
                        reader.issue(file, position, "account used by only one companion", ref)
                        global_errors.append(reader.errors[-1])
                        for previous_index, previous in enumerate(statuses):
                            if previous["id"] == account_users[ref]:
                                statuses[previous_index] = freeze({**previous, "valid": False})
                    account_users[ref] = companion_id
            references.extend(
                (channel.get("plugin"), ("channels", index), channel.get("config"), "channels")
                for index, channel in enumerate(channels)
                if isinstance(channel, dict)
            )
        plugin_settings = _entries(data.get("plugins"))
        for key, point in (
            ("embedder", "embedder"),
            ("reranker", "memory.reranker"),
            ("consolidator", "memory.consolidator"),
        ):
            memory_plugin = (
                memory.get("recollect", {}).get("consolidator")
                if key == "consolidator"
                else memory.get(key)
                if isinstance(memory, dict)
                else None
            )
            if key == "consolidator" and memory_plugin == default_consolidator():
                continue
            if memory_plugin is None:
                continue
            position = ("memory", key)
            setting = (
                plugin_settings.get(memory_plugin, {}) if isinstance(memory_plugin, str) else {}
            )
            if not isinstance(setting, dict) or setting.get("enabled") is not True:
                reader.issue(file, position, "memory plugin explicitly enabled", memory_plugin)
            plugin, _, state = manifest(memory_plugin, file, position)
            if state == "found" and plugin is not None:
                contributes = plugin.get("contributes", {})
                declared = (
                    contributes.get("embedder")
                    if key == "embedder"
                    else contributes.get("memory", {}).get(key)
                )
                if not declared:
                    reader.issue(file, position, f"plugin implements {point}", memory_plugin)
        role_plugins = {ref[0] for ref in references if isinstance(ref[0], str)}
        references.extend(
            (key, ("plugins", key), value.get("config"), "plugins")
            for key, value in plugin_settings.items()
            if isinstance(value, dict) and value.get("enabled") is True
        )
        listed_plugins = {ref[0] for ref in references if ref[3] == "plugins"}
        references.extend(
            (key, ("plugins", key), {}, "plugins") for key in sorted(role_plugins - listed_plugins)
        )
        declared_tools = set()
        merged_for_companion: dict[str, Any] = {}
        model_config = recollect_model_config = None
        channel_configs: dict[int, Any] = {}
        for plugin_id, position, inline, kind in references:
            setting = plugin_settings.get(plugin_id, {}) if isinstance(plugin_id, str) else {}
            if kind != "plugins" and isinstance(setting, dict) and setting.get("enabled") is False:
                reader.issue(
                    file,
                    ("plugins", plugin_id, "enabled"),
                    "referenced model/channel must be enabled",
                    False,
                )
            plugin, plugin_file, state = manifest(
                plugin_id, file, position + (("plugin",) if kind != "plugins" else ())
            )
            item = {"id": plugin_id, "status": state}
            if plugin and isinstance(plugin.get("backup", {}), dict):
                item["backup_mode"] = plugin.get("backup", {}).get("mode", "unsupported")
            if kind == "recollect_model":
                status["plugins"].append(item)
            elif kind == "model":
                status[kind] = item
            else:
                status[kind].append(item)
            if state != "found" or plugin is None:
                continue
            if kind == "plugins" and plugin_id in role_plugins:
                # 同一模型/平台由各账号的第四层最终配置校验，不提前校验未完整的三层。
                continue
            declared_tools.update(t["name"] for t in plugin.get("contributes", {}).get("tools", []))
            schema = plugin["config"]
            defaults, origins = _default_config(schema, plugin_file)
            instance_settings = _entries(instance.get("plugins")).get(plugin_id, {})
            companion_settings = plugin_settings.get(plugin_id, {})
            instance_config = (
                instance_settings.get("config", {}) if isinstance(instance_settings, dict) else {}
            )
            companion_config = (
                companion_settings.get("config", {}) if isinstance(companion_settings, dict) else {}
            )
            layers_to_merge = [(defaults, plugin_file, ("config", "default"))]
            overrides = []
            if isinstance(instance_config, dict):
                overrides.append((instance_config, instance_file, ("plugins", plugin_id, "config")))
            if isinstance(companion_config, dict):
                overrides.append((companion_config, file, ("plugins", plugin_id, "config")))
            base_merged = deep_merge(defaults, *(layer for layer, _, _ in overrides))
            merged_for_companion[plugin_id] = base_merged
            if kind != "plugins" and isinstance(inline, dict):
                overrides.append((inline, file, position + ("config",)))
            global_origins = []
            for override in overrides:
                previous_origins = list(origins.values())
                _merge_origins([override], origins)
                if override[0] is instance_config:
                    global_origins.extend(
                        value
                        for value in origins.values()
                        if not any(value is old for old in previous_origins)
                    )
            merged = deep_merge(*(layer for layer, _, _ in layers_to_merge + overrides))
            reader.validate(
                thaw(merged),
                schema,
                plugin_file,
                ("config", "default"),
                origins,
                global_origins=global_origins,
                global_errors=global_errors,
            )
            if kind == "recollect_model":
                recollect_model_config = merged
            elif kind == "model":
                model_config = merged
            elif kind == "channels":
                channel_configs[position[1]] = merged
        for tool in _entries(data.get("tool_scopes")):
            if tool not in declared_tools:
                dialogue_notices.append(
                    {"companion_id": companion_id, "code": "tool_scope_unavailable", "tool": tool}
                )
        if isinstance(boundary, dict) and boundary.get("admission") == "owner_present":
            dialogue_notices.append(
                {
                    "companion_id": companion_id,
                    "code": "owner_present_requires_membership",
                    "message": "不支持成员查询的平台账号只对登记的群生效",
                }
            )
        configurations[companion_id] = freeze(
            {
                "config": data,
                "plugins": merged_for_companion,
                "model": model_config,
                "recollect_model": recollect_model_config,
                "channels": channel_configs,
            }
        )
        status["valid"] = (
            not instance_errors
            and len(reader.errors) == start
            and all(
                item["status"] == "found"
                for item in ([status["model"]] if status["model"] else [])
                + status["channels"]
                + status["plugins"]
            )
        )
        statuses.append(freeze(status))
    current_companion = None
    notify_start = len(reader.errors)
    notify = instance.get("notify", {})
    notify_ids = notify.get("plugins", []) if isinstance(notify, dict) else []
    for index, pid in enumerate(notify_ids if isinstance(notify_ids, list) else []):
        if not isinstance(pid, str):
            continue
        data, file, state = manifest(pid, instance_file, ("notify", "plugins", index))
        if data is None or "notifier" not in data.get("contributes", {}):
            reader.issue(
                instance_file, ("notify", "plugins", index), "plugin declaring notifier", pid
            )
    global_errors.extend(reader.errors[notify_start:])
    for pid, users in manifest_users.items():
        if len(users) > 1 or None in users:
            global_errors.extend(e for e in manifest_errors.get(pid, ()) if e not in global_errors)
    return HomeReport(
        str(directory),
        True,
        tuple(statuses),
        tuple(reader.errors),
        freeze(instance),
        freeze(configurations),
        global_errors=tuple(global_errors),
        dialogue_notices=tuple(dialogue_notices),
    )


__all__ = ["ConfigIssue", "HomeReport", "deep_merge", "freeze", "load_home", "resolve_home", "thaw"]
