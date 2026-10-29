"""清单发现与依赖解析只使用手写虚构插件目录。"""

import json
from pathlib import Path

import pytest

from clytie.plugin_host.dependencies import DependencyError, resolve
from clytie.plugin_host.manifests import ManifestError, discover, read_manifest

ROOT = Path(__file__).resolve().parents[1]


def make_manifest(root, plugin_id, **overrides):
    data = {
        "schema_version": 1,
        "id": plugin_id,
        "name": "示例插件",
        "summary": "手写虚构插件",
        "category": "example",
        "version": "1.0.0",
        "requires": {"sdk": ">=0.5,<0.6"},
        "entry": "plugin:Plugin",
        "contributes": {},
        "permissions": [],
        "config": {"type": "object", "additionalProperties": False},
        **overrides,
    }
    directory = root / plugin_id
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "plugin.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    (directory / "plugin.py").write_text(
        'raise RuntimeError("must not execute")\n', encoding="utf-8"
    )
    return path


def test_discovery_does_not_execute_and_rejects_duplicate(tmp_path):
    make_manifest(tmp_path / "official", "plugin-a")
    items = discover(tmp_path / "official", repo_root=ROOT)
    assert list(items) == ["plugin-a"]
    with pytest.raises(TypeError):
        items["plugin-a"].data["version"] = "2.0.0"
    make_manifest(tmp_path / "installed", "plugin-a")
    with pytest.raises(ManifestError, match="globally unique"):
        discover(tmp_path / "official", tmp_path / "installed", repo_root=ROOT)


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"id": "other-plugin"}, "/id"),
        ({"requires": {"sdk": ">=9"}}, "/requires/sdk"),
        ({"requires": {"sdk": "invalid"}}, "/requires/sdk"),
        ({"requires": 1}, "/requires"),
        ({"unknown": True}, "/unknown"),
        ({"config": {"type": "invalid"}}, "/config"),
        ({"config": {"$ref": "https://example.invalid/schema"}}, "/config/$ref"),
    ],
)
def test_bad_manifests_report_file_and_field(tmp_path, overrides, field):
    path = make_manifest(tmp_path, "plugin-a", **overrides)
    with pytest.raises(ManifestError) as exc:
        read_manifest(path, repo_root=ROOT)
    assert any(issue.file == str(path) and issue.pointer == field for issue in exc.value.issues)


def test_plan_is_topological_and_does_not_enable_missing_optional(tmp_path):
    make_manifest(tmp_path, "plugin-a", dependencies={"plugin-b": {"version": ">=1"}})
    make_manifest(
        tmp_path,
        "plugin-b",
        dependencies={"plugin-c": {"version": ">=1", "optional": True}},
    )
    items = discover(tmp_path, repo_root=ROOT)
    plan = resolve(items, ["plugin-a"])
    assert plan.order == ("plugin-b", "plugin-a")
    assert plan.additional == ("plugin-b",)
    assert resolve(items, reversed(["plugin-a", "plugin-b"])).order == plan.order


@pytest.mark.parametrize("case", ["missing", "version", "conflict", "cycle"])
def test_dependency_errors_have_reason_and_remediation(tmp_path, case):
    make_manifest(tmp_path, "plugin-a", dependencies={"plugin-b": {"version": ">=1"}})
    if case == "version":
        make_manifest(tmp_path, "plugin-b", version="0.5.0")
    elif case == "conflict":
        make_manifest(tmp_path, "plugin-b", conflicts=["plugin-a"])
    elif case == "cycle":
        make_manifest(tmp_path, "plugin-b", dependencies={"plugin-a": {"version": ">=1"}})
    with pytest.raises(DependencyError) as exc:
        resolve(discover(tmp_path, repo_root=ROOT), ["plugin-a"])
    text = str(exc.value)
    assert "plugin-a" in text and "plugin-b" in text
    assert {"missing": "install", "version": "compatible", "conflict": "disable", "cycle": "cycle"}[
        case
    ] in text
