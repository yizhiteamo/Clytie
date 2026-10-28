"""纯函数依赖计划：拒绝冲突，按拓扑顺序列出需要确认启用的插件。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from packaging.specifiers import SpecifierSet

from .manifests import Manifest


class DependencyError(ValueError):
    """包含依赖链和补齐建议的计划失败。"""


@dataclass(frozen=True)
class EnablePlan:
    requested: tuple[str, ...]
    order: tuple[str, ...]
    additional: tuple[str, ...]


def resolve(
    manifests: Mapping[str, Manifest], requested: Iterable[str], *, active: Iterable[str] = ()
) -> EnablePlan:
    requested = tuple(sorted(set(requested)))
    selected = set(requested) | set(active)
    stack: list[str] = []
    visited: set[str] = set()
    order: list[str] = []

    def visit(plugin_id: str) -> None:
        if plugin_id in stack:
            raise DependencyError("dependency cycle: " + " -> ".join([*stack, plugin_id]))
        if plugin_id in visited:
            return
        manifest = manifests.get(plugin_id)
        if manifest is None:
            raise DependencyError(
                " -> ".join([*stack, plugin_id])
                + f": missing dependency; install {plugin_id} and retry the enable plan"
            )
        stack.append(plugin_id)
        for dependency, spec in sorted(manifest.data.get("dependencies", {}).items()):
            if dependency.startswith("x-"):
                continue
            if spec.get("optional", False) and dependency not in selected:
                continue
            target = manifests.get(dependency)
            if target is None:
                visit(dependency)
            if target.version not in SpecifierSet(spec["version"]):
                raise DependencyError(
                    f"{plugin_id} requires {dependency}{spec['version']}; found {target.version}; "
                    "install a compatible version before enabling"
                )
            selected.add(dependency)
            visit(dependency)
        stack.pop()
        visited.add(plugin_id)
        order.append(plugin_id)

    # 必需依赖闭包先收齐，再检查可选依赖边，避免遍历顺序影响结果。
    pending = list(sorted(selected))
    while pending:
        item = pending.pop()
        if item not in manifests:
            visit(item)
        for dependency, spec in manifests[item].data.get("dependencies", {}).items():
            if not dependency.startswith("x-") and not spec.get("optional", False):
                if dependency not in manifests:
                    raise DependencyError(
                        f"{item} -> {dependency}: missing dependency; "
                        f"install {dependency}{spec['version']} and retry the enable plan"
                    )
                if dependency not in selected:
                    selected.add(dependency)
                    pending.append(dependency)
    for plugin_id in sorted(selected):
        visit(plugin_id)
    for plugin_id in order:
        for conflict in manifests[plugin_id].data.get("conflicts", ()):
            if conflict in selected:
                raise DependencyError(f"{plugin_id} conflicts with {conflict}; disable one first")
    return EnablePlan(
        requested, tuple(order), tuple(sorted(selected - set(requested) - set(active)))
    )
