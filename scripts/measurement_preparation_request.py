"""Narrow preparation request gate. No generator, uploader, run counter or rerun.

CI validates its event and checkout, not the live PR head or globally unique
execution. The local operator must check those with existing account access.
"""

import os
import platform
import stat
from pathlib import Path

try:
    from . import measurement_evidence as evidence
    from . import measurement_preparation_evidence as prep
    from . import measurement_preparation_transport as transport
    from . import measurement_transport as base
    from . import measurement_version as versions
except ImportError:
    import measurement_evidence as evidence
    import measurement_preparation_evidence as prep
    import measurement_preparation_transport as transport
    import measurement_transport as base
    import measurement_version as versions

Error = evidence.EvidenceError
ROOT = base.ROOT
REQUEST_PATH = ".github/a43-preparation-request.json"
PUBLIC_PATH = ".github/a43-preparation-public.json"
SCALES = ("small_text", "large_database", "many_small_files", "large_asset")


def _repository(value, identifier):
    if (
        type(value) is not dict
        or value.get("full_name") != base.REPOSITORY
        or type(value.get("id")) is not int
        or value["id"] != identifier
        or value.get("private") is not True
        or value.get("fork") is not False
    ):
        raise Error("preparation_repository_mismatch")


def validate_request(root, event, env, *, repository=base.repository, system=None):
    system = platform.system() if system is None else system
    if (
        system not in {"Linux", "Windows"}
        or env.get("GITHUB_ACTIONS") != "true"
        or env.get("GITHUB_EVENT_NAME") != "pull_request"
        or env.get("GITHUB_REPOSITORY") != base.REPOSITORY
        or env.get("GITHUB_RUN_ATTEMPT") != "1"
        or env.get("GITHUB_JOB") != "prepare_" + system.lower()
    ):
        raise Error("preparation_ci_identity_invalid")
    evidence._pattern(env.get("GITHUB_RUN_ID"), r"[1-9][0-9]{0,19}")
    identifier = event["repository"]["id"]
    evidence._number(identifier, integer=True, positive=True)
    if env.get("GITHUB_REPOSITORY_ID") != str(identifier):
        raise Error("preparation_repository_mismatch")
    pr = event["pull_request"]
    if event.get("action") != "synchronize" or pr.get("state") != "open":
        raise Error("preparation_event_invalid")
    for value in (event["number"], pr["number"]):
        evidence._number(value, integer=True, positive=True)
    if event["number"] != pr["number"]:
        raise Error("preparation_event_invalid")
    for value in (event["repository"], pr["head"]["repo"], pr["base"]["repo"]):
        _repository(value, identifier)
    if pr["base"]["ref"] != "main":
        raise Error("preparation_base_invalid")
    head = versions._git(root, "rev-parse", "HEAD").decode("ascii").strip()
    evidence._pattern(head, r"[a-f0-9]{40}")
    parents = versions._git(root, "rev-list", "--parents", "-n", "1", "HEAD").decode().split()
    if len(parents) != 2 or parents[0] != head:
        raise Error("preparation_single_parent_required")
    parent = parents[1]
    evidence._pattern(parent, r"[a-f0-9]{40}")
    evidence._pattern(event.get("before"), r"[a-f0-9]{40}")
    if event.get("after") != head or pr["head"]["sha"] != head:
        raise Error("preparation_event_head_mismatch")
    changed = versions._git(
        root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", parent, head, "--"
    )
    if changed != REQUEST_PATH.encode() + b"\0":
        # PR path filtering is cumulative. Ordinary code pushes must not create
        # fake request failures; any new request mixed into this push still fails.
        if REQUEST_PATH.encode() in changed.split(b"\0") or versions._git(
            root, "diff", "--name-only", "-z", event["before"], head, "--", REQUEST_PATH
        ):
            raise Error("preparation_request_only_commit_required")
        return None
    if event["before"] != parent:
        raise Error("preparation_event_head_mismatch")
    if versions._git(root, "diff", "HEAD", "--name-only", "--").strip():
        raise Error("preparation_worktree_modified")
    request = prep._bounded_json(prep._read_file(Path(root) / REQUEST_PATH, 4096))
    evidence._fields(
        request,
        (
            "format",
            "purpose",
            "parent",
            "pull_request",
            "fixture_version",
            "seed",
            "scales",
            "public_sha256",
        ),
    )
    evidence._number(request["pull_request"], integer=True, positive=True)
    evidence._pattern(request["parent"], r"[a-f0-9]{40}")
    evidence._pattern(request["public_sha256"], r"[a-f0-9]{64}")
    if (
        type(request["format"]) is not int
        or request["format"] != 1
        or request["purpose"] != prep.PURPOSE
        or request["parent"] != parent
        or request["pull_request"] != event["number"]
        or pr["number"] != event["number"]
        or request["fixture_version"] != "legal_memory_v2"
        or type(request["seed"]) is not int
        or request["seed"] != 73
        or request["scales"] != list(SCALES)
    ):
        raise Error("preparation_request_mismatch")
    public_raw = prep._read_file(Path(root) / PUBLIC_PATH, 4096)
    if prep.digest(public_raw) != request["public_sha256"]:
        raise Error("preparation_public_mismatch")
    public = prep._bounded_json(public_raw)
    evidence._fields(public, ("format", "recipient", "fingerprint", "tool", "version"))
    if (
        type(public["format"]) is not int
        or public["format"] != 1
        or public["tool"] != "pyrage"
        or public["version"] != "1.4.0"
        or public["fingerprint"] != evidence.fingerprint(public["recipient"])
    ):
        raise Error("preparation_public_mismatch")
    # Metadata is available with existing repository read access. No PR/run API,
    # write permission, private list, private key or event dump is needed here.
    transport._private(repository)
    return head


def main():
    try:
        event = evidence._json(prep._read_file(os.environ["GITHUB_EVENT_PATH"], 1024 * 1024))
        candidate = validate_request(ROOT, event, os.environ)
        if candidate is not None:
            evidence._pattern(candidate, r"[a-f0-9]{40}")
        path = prep._safe(Path(os.environ["GITHUB_OUTPUT"]))
        before = path.stat()
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise Error("invalid_output_file")
        descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as output:
            opened = os.fstat(output.fileno())
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
            ):
                raise Error("output_file_changed")
            output.write("eligible=" + ("true" if candidate is not None else "false") + "\n")
        print(
            "preparation_request_eligible_not_measurement"
            if candidate is not None
            else "preparation_not_requested"
        )
        return 0
    except Exception:
        print("preparation_request_rejected")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
