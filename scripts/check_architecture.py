"""Enforce SDK-only plugin imports and injected time/random dependencies."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = {
    "time.time",
    "time.sleep",
    "time.monotonic",
    "time.perf_counter",
    "datetime.datetime.now",
    "datetime.datetime.utcnow",
    "datetime.datetime.today",
    "datetime.date.today",
    "asyncio.sleep",
    "asyncio.timeout",
    "asyncio.timeout_at",
    "asyncio.wait_for",
}


def inspect_source(
    source: str, *, plugin: bool = False, relative_path: str = ""
) -> list[tuple[int, str]]:
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    errors: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name.split(".")[0]] = (
                    item.name if item.asname else item.name.split(".")[0]
                )
                modules.append(item.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for item in node.names:
                aliases[item.asname or item.name] = f"{module}.{item.name}"
                modules.append(f"{module}.{item.name}")
        for module in modules:
            if plugin and (
                (
                    module.startswith("clytie.")
                    and module != "clytie.sdk"
                    and not module.startswith("clytie.sdk.")
                )
                or module == "clytie"
                or module.startswith("plugins.")
            ):
                errors.append((node.lineno, "plugin-import-outside-sdk"))

    def qualified(node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return aliases.get(node.id, node.id)
        if isinstance(node, ast.Attribute):
            return f"{qualified(node.value)}.{node.attr}"
        return ""

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = qualified(node.func)
            system_adapter = (
                not plugin
                and relative_path == "src/clytie/clock.py"
                and name in {"datetime.datetime.now", "time.monotonic", "asyncio.sleep"}
            )
            if name in FORBIDDEN and not system_adapter:
                errors.append((node.lineno, "clock-must-be-injected"))
            if name.startswith("random.") and name != "random.Random":
                errors.append((node.lineno, "random-must-be-injected"))
    return sorted(set(errors))


def main() -> int:
    failures = 0
    count = 0
    for folder in (ROOT / "src", ROOT / "plugins"):
        for path in sorted(folder.rglob("*.py")):
            count += 1
            relative = path.relative_to(ROOT)
            for line, message in inspect_source(
                path.read_text(encoding="utf-8"),
                plugin=relative.parts[0] == "plugins",
                relative_path=relative.as_posix(),
            ):
                print(f"{relative.as_posix()}:{line}: {message}")
                failures += 1
    if not failures:
        print(f"SDK import and injected clock/random checks passed: {count} files")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
