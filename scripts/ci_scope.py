"""Classify changed paths; uncertain comparisons always require the full CI suite."""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def classify_files(files):
    if not isinstance(files, list) or not files:
        return "full"
    for path in files:
        if not isinstance(path, str) or "\\" in path:
            return "full"
        parts = path.split("/")
        if any(part in ("", ".", "..") for part in parts):
            return "full"
        if len(parts) > 1 and parts[0] == "docs":
            continue
        if len(parts) == 1 and path.endswith(".md"):
            continue
        return "full"
    return "docs"


def github_scope(event_name, event_path, root):
    if event_name != "pull_request":
        return "full"
    try:
        event = json.loads(Path(event_path).read_text(encoding="utf-8"))
        pr = event["pull_request"]
        base, head = pr["base"]["sha"], pr["head"]["sha"]
        if not all(
            isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{40}", sha) for sha in (base, head)
        ):
            return "full"
        # Disable rename detection: moving code into docs must include its deleted source.
        result = subprocess.run(
            [
                "git",
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-only",
                "-z",
                f"{base}...{head}",
                "--",
            ],
            cwd=root,
            capture_output=True,
            check=True,
            timeout=30,
        )
        raw = result.stdout.decode("utf-8")
        if not raw or not raw.endswith("\0"):
            return "full"
        return classify_files(raw[:-1].split("\0"))
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return "full"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--github", action="store_true", help="Compare the fixed PR event commits")
    args = parser.parse_args()
    if args.github:
        scope = github_scope(
            os.environ.get("GITHUB_EVENT_NAME"), os.environ.get("GITHUB_EVENT_PATH"), Path.cwd()
        )
    else:
        try:
            scope = classify_files(json.load(sys.stdin))
        except (OSError, ValueError):
            scope = "full"
    print(scope)


if __name__ == "__main__":
    main()
