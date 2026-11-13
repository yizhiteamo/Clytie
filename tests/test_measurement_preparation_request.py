"""Fictional event metadata and tiny temporary git only; never runs geometry."""

import copy
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from tests.measurement_tools import load

ROOT = Path(__file__).resolve().parents[1]
gate = load("measurement_preparation_request")


def test_gate_entry_exists():
    assert callable(getattr(gate, "validate_request", None))


def git(root, *args):
    return (
        subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=fixture",
                "-c",
                "user.email=" + "fixture@users.noreply.github.com",
                "-c",
                "core.hooksPath=" + str(root / "no-hooks"),
                *args,
            ],
            cwd=root,
            env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
            stderr=subprocess.PIPE,
        )
        .decode()
        .strip()
    )


def fixture(tmp_path):
    root = tmp_path / "repository"
    root.mkdir()
    (root / "no-hooks").mkdir()
    git(root, "init", "-q")
    public_path = root / gate.PUBLIC_PATH
    public_path.parent.mkdir()
    identity = gate.evidence.native_identity().generate()
    recipient = str(identity.to_public())
    public = dict(
        format=1,
        recipient=recipient,
        fingerprint=gate.evidence.fingerprint(recipient),
        tool="pyrage",
        version="1.4.0",
    )
    public_path.write_bytes(gate.prep.canonical(public))
    (root / "source.py").write_bytes(b"fictional = True\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fictional preparation parent")
    parent = git(root, "rev-parse", "HEAD")
    request = dict(
        format=1,
        purpose=gate.prep.PURPOSE,
        parent=parent,
        pull_request=42,
        fixture_version="legal_memory_v2",
        seed=73,
        scales=list(gate.SCALES),
        public_sha256=gate.prep.digest(public_path.read_bytes()),
    )
    (root / gate.REQUEST_PATH).write_bytes(gate.prep.canonical(request))
    git(root, "add", gate.REQUEST_PATH)
    git(root, "commit", "-qm", "fictional preparation request")
    head = git(root, "rev-parse", "HEAD")
    repository = dict(id=7, full_name=gate.base.REPOSITORY, private=True, fork=False)
    pr = dict(
        number=42,
        state="open",
        head=dict(sha=head, repo=copy.deepcopy(repository)),
        base=dict(ref="main", repo=copy.deepcopy(repository)),
    )
    event = dict(
        action="synchronize",
        number=42,
        before=parent,
        after=head,
        repository=copy.deepcopy(repository),
        pull_request=pr,
    )
    env = dict(
        GITHUB_ACTIONS="true",
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_REPOSITORY=gate.base.REPOSITORY,
        GITHUB_RUN_ID="123",
        GITHUB_REPOSITORY_ID="7",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_JOB="prepare_linux",
    )
    return root, request, event, env


def private():
    return dict(full_name=gate.base.REPOSITORY, private=True)


def test_request_only_commit_allowed(tmp_path):
    root, request, event, env = fixture(tmp_path)
    result = gate.validate_request(root, event, env, repository=private, system="Linux")
    assert result == event["after"]
    assert request["parent"] != result


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_EVENT_NAME", "workflow_dispatch"),
        ("GITHUB_JOB", "prepare_windows"),
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_REPOSITORY", "other/repository"),
    ],
)
def test_wrong_ci_identity(tmp_path, field, value):
    root, _, event, env = fixture(tmp_path)
    env[field] = value
    with pytest.raises(gate.Error):
        gate.validate_request(root, event, env, repository=private, system="Linux")


@pytest.mark.parametrize("kind", ["fork", "head", "before", "after", "pr", "repository", "closed"])
def test_wrong_event(tmp_path, kind):
    root, _, event, env = fixture(tmp_path)
    if kind == "fork":
        event["pull_request"]["head"]["repo"]["fork"] = True
    elif kind == "head":
        event["pull_request"]["head"]["sha"] = "0" * 40
    elif kind in {"before", "after"}:
        event[kind] = "0" * 40
    elif kind == "pr":
        event["number"] += 1
    elif kind == "repository":
        event["pull_request"]["head"]["repo"]["id"] += 1
    elif kind == "closed":
        event["pull_request"]["state"] = "closed"
    with pytest.raises(gate.Error):
        gate.validate_request(root, event, env, repository=private, system="Linux")


@pytest.mark.parametrize("kind", ["extra_file", "dirty", "public", "request"])
def test_only_explicit_request_change_allowed(tmp_path, kind):
    root, request, event, env = fixture(tmp_path)
    if kind == "public":
        (root / gate.PUBLIC_PATH).write_bytes(b"{}")
    elif kind == "request":
        request["seed"] = 74
        (root / gate.REQUEST_PATH).write_bytes(gate.prep.canonical(request))
    else:
        (root / "source.py").write_bytes(b"changed = True\n")
    if kind == "extra_file":
        git(root, "add", ".")
        git(root, "commit", "-qm", "fictional disallowed change")
        if kind == "extra_file":
            git(root, "reset", "--soft", "HEAD~2")
            git(root, "commit", "-qm", "fictional mixed commit")
        event["after"] = git(root, "rev-parse", "HEAD")
        event["pull_request"]["head"]["sha"] = event["after"]
    with pytest.raises(gate.Error):
        gate.validate_request(root, event, env, repository=private, system="Linux")


def test_normal_push_without_new_request_is_not_requested(tmp_path):
    root, _, event, env = fixture(tmp_path)
    event["before"] = event["after"]
    (root / "source.py").write_bytes(b"normal_change = True\n")
    git(root, "add", "source.py")
    git(root, "commit", "-qm", "fictional ordinary code push")
    event["after"] = git(root, "rev-parse", "HEAD")
    event["pull_request"]["head"]["sha"] = event["after"]
    assert gate.validate_request(root, event, env, repository=private, system="Linux") is None
    env["GITHUB_RUN_ATTEMPT"] = "2"
    with pytest.raises(gate.Error):
        gate.validate_request(root, event, env, repository=private, system="Linux")


def test_new_request_earlier_in_same_push_is_not_silently_skipped(tmp_path):
    root, _, event, env = fixture(tmp_path)
    (root / "source.py").write_bytes(b"later_change = True\n")
    git(root, "add", "source.py")
    git(root, "commit", "-qm", "fictional batch push after request")
    event["after"] = git(root, "rev-parse", "HEAD")
    event["pull_request"]["head"]["sha"] = event["after"]
    with pytest.raises(gate.Error):
        gate.validate_request(root, event, env, repository=private, system="Linux")


@pytest.mark.parametrize(
    "result,code,output",
    [(None, 0, "eligible=false\n"), ("a" * 40, 0, "eligible=true\n"), ("error", 1, "")],
)
def test_main_distinguishes_skip_success_and_error(tmp_path, monkeypatch, result, code, output):
    event = tmp_path / "event.json"
    event.write_bytes(b"{}")
    target = tmp_path / "output"
    target.write_bytes(b"")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_OUTPUT", str(target))

    def validate(*args):
        if result == "error":
            raise gate.Error("preparation_request_mismatch")
        return result

    monkeypatch.setattr(gate, "validate_request", validate)
    assert gate.main() == code
    assert target.read_text() == output


@pytest.mark.parametrize("value", [False, None, "true"])
def test_private_required(tmp_path, value):
    root, _, event, env = fixture(tmp_path)
    with pytest.raises(gate.Error):
        gate.validate_request(
            root,
            event,
            env,
            repository=lambda: dict(full_name=gate.base.REPOSITORY, private=value),
            system="Linux",
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", True),
        ("seed", 74),
        ("purpose", "performance"),
        ("fixture_version", "unknown"),
        ("scales", ["small_text"]),
        ("public_sha256", "0" * 64),
        ("pull_request", 43),
        ("unexpected", "field"),
    ],
)
def test_committed_request_schema_fail_closed(tmp_path, field, value):
    root, request, event, env = fixture(tmp_path)
    request["parent"] = event["after"]
    event["before"] = event["after"]
    request[field] = value
    (root / gate.REQUEST_PATH).write_bytes(gate.prep.canonical(request))
    git(root, "add", gate.REQUEST_PATH)
    git(root, "commit", "-qm", "fictional invalid request")
    event["after"] = git(root, "rev-parse", "HEAD")
    event["pull_request"]["head"]["sha"] = event["after"]
    with pytest.raises(gate.Error):
        gate.validate_request(root, event, env, repository=private, system="Linux")


def test_request_has_no_global_deduplication_claim(tmp_path):
    root, _, event, env = fixture(tmp_path)
    first = gate.validate_request(root, event, env, repository=private, system="Linux")
    env["GITHUB_RUN_ID"] = "124"
    # Same request can validate again in another first-attempt run. The local
    # operator's run-history check is required; this test preserves the limit.
    assert gate.validate_request(root, event, env, repository=private, system="Linux") == first


def test_workflow_scope_and_permissions():
    workflow = yaml.safe_load((ROOT / ".github/workflows/preparation.yml").read_text())
    # PyYAML's YAML 1.1 resolver treats unquoted `on` as True; draft quotes it.
    assert workflow["on"] == {
        "pull_request": {"types": ["synchronize"], "paths": [gate.REQUEST_PATH]}
    }
    assert workflow["permissions"] == {"contents": "read"}
    assert set(workflow["jobs"]) == {"prepare_linux", "prepare_windows"}
    for name, job in workflow["jobs"].items():
        assert "permissions" not in job
        steps = job["steps"]
        assert steps[0]["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
        texts = "\n".join(step.get("run", "") for step in steps)
        assert "measurement_preparation_request" in texts
        assert "measurement_preparation_generate prepare" in texts
        assert "measurement_preparation_transport ci-seal" in texts
        assert "measurement_trial" not in texts and "measurement_geometry" not in texts
        for step in steps:
            if any(
                name in step.get("run", "")
                for name in (
                    "measurement_preparation_generate",
                    "measurement_preparation_transport",
                )
            ):
                assert "steps.request.outputs.eligible == 'true'" in step["if"]
        upload = steps[-1]
        assert upload["with"]["retention-days"] == 14
        assert upload["with"]["path"].splitlines() == [
            "${{ steps.seal.outputs.ciphertext }}",
            "${{ steps.seal.outputs.receipt }}",
        ]
        assert "always" not in upload["if"]
        assert job["env"]["PREPARATION_PUBLIC"] == gate.PUBLIC_PATH
