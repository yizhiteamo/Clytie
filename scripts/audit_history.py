"""Scan all reachable Git text blobs, paths, identities and messages in memory."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parents[1]
REGULAR_FILE_MODES = {format(0o100_644, "o"), format(0o100_755, "o")}
GITHUB_WEB_COMMITTER = ("GitHub", "@".join(("noreply", "github.com")))


def git(*args: str) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    ).stdout


def main() -> int:
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    program = shlex.split(config["repos"][0]["hooks"][0]["entry"])[2]
    namespace: dict = {}
    with patch.object(sys, "argv", ["scan", "--private"]):
        try:
            exec(compile(program, "repository-safety", "exec"), namespace)
        except SystemExit as result:
            if result.code:
                return 1
    terms = namespace["denylist"]
    normalize = namespace["normalized"]
    public = namespace["public_categories"]
    file_categories = namespace["file_categories"]
    commits = git("rev-list", "--all").decode().splitlines()
    seen = set()
    failures = 0

    def scan(
        text: str, label: str, *, lock: bool = False, official_committer: bool = False
    ) -> None:
        nonlocal failures
        for number, line in enumerate(text.splitlines(), 1):
            categories = public(line, lock_metadata=lock)
            # 只豁免精确官方提交者的邮箱字段；私有扫描和其他告警仍执行。
            if official_committer and number == 2:
                categories.discard("possible-personal-email")
            if any(term in normalize(line) for term in terms):
                categories.add("private-content")
            for category in sorted(categories):
                failures += 1
                print(f"{label}:{number}: {category}")

    for commit in commits:
        metadata = git("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B", commit).decode("utf-8")
        fields = metadata.split("\n", 4)
        if len(fields) == 5:
            author_name, author_email, committer_name, committer_email, message = fields
            scan(f"{author_name}\n{author_email}", f"commit:{commit}:author")
            scan(
                f"{committer_name}\n{committer_email}",
                f"commit:{commit}:committer",
                official_committer=(committer_name, committer_email) == GITHUB_WEB_COMMITTER,
            )
            scan(message, f"commit:{commit}:message")
        else:
            scan(metadata, f"commit:{commit}:malformed-metadata")
        entries = git("ls-tree", "-r", "-z", commit).split(b"\0")
        for entry in entries:
            if not entry:
                continue
            meta, raw_path = entry.split(b"\t", 1)
            mode, kind, oid = meta.decode().split()
            label = f"blob:{oid}"
            try:
                path = raw_path.decode("utf-8")
            except UnicodeError:
                failures += 1
                print(f"{label}: non-utf8-history-path")
                continue
            # 模式属于每次树引用；同内容/路径曾作为符号链接时也必须拒绝。
            if kind != "blob" or mode not in REGULAR_FILE_MODES:
                scan(path, label + ":path")
                failures += 1
                print(f"{label}: unsupported-history-entry")
                continue
            key = (oid, path)
            if key in seen:
                continue
            seen.add(key)
            scan(path, label + ":path")
            size = int(git("cat-file", "-s", oid))
            file_issues = file_categories(path, size)
            for category in sorted(file_issues):
                failures += 1
                print(f"{label}: {category}")
            if "file-over-one-mib" in file_issues:
                continue
            try:
                content = git("cat-file", "blob", oid).decode("utf-8-sig")
            except UnicodeError:
                failures += 1
                print(f"{label}: non-text-history-entry")
                continue
            if "\0" in content:
                failures += 1
                print(f"{label}: binary-content")
                continue
            scan(content, label, lock=path == "uv.lock")
    if not failures:
        print(f"History scan passed: {len(commits)} commits, {len(seen)} unique blob/path pairs")
        print(
            "Checked file policies, contents, paths, author/committer identities and commit messages"
        )
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
