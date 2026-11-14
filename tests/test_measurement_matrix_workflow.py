"""Pure orchestration tests; external commands and real trial processes are blocked."""

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from tests import test_measurement_plan_ingress as ingress_tests
from tests.measurement_tools import load as load_tool

plan = load_tool("measurement_plan")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def block_external(monkeypatch):
    original = subprocess.Popen
    allowed = {
        "init",
        "config",
        "add",
        "commit",
        "rev-parse",
        "rev-list",
        "diff-tree",
        "diff",
        "show",
        "ls-tree",
        "cat-file",
        "archive",
        "fetch",
        "checkout",
    }

    def popen(args, *rest, **kwargs):
        if (
            type(args) is not list
            or args[0] != "git"
            or args[1] not in allowed
            or any("https://" in str(item) or "http://" in str(item) for item in args)
        ):
            raise AssertionError("external command or trial process forbidden")
        return original(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)


@pytest.fixture
def module(monkeypatch):
    load_tool("measurement_matrix_workflow")
    return load_module(
        "mechanism_workflow_under_test",
        Path(__file__).resolve().parents[1] / "scripts/measurement_matrix_workflow.py",
    )


def frozen_plan():
    from tests.test_measurement_matrix_plan import prepared

    return prepared()[0]


@pytest.fixture
def setup(module, monkeypatch, tmp_path):
    m = module
    frozen = frozen_plan()
    env = dict(
        RUNNER_TEMP=str(tmp_path),
        GITHUB_ACTIONS="true",
        GITHUB_REPOSITORY=m.transport.REPOSITORY,
        GITHUB_RUN_ID="123",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_JOB=next(iter(m.matrix.jobs(frozen).values())),
    )
    monkeypatch.setattr(m.platform, "system", lambda: "Windows")

    def repository():
        return dict(full_name=m.transport.REPOSITORY, private=True)

    target = m.data_root(env)
    target.mkdir()
    for path in m.sources(target).values():
        path.mkdir(parents=True)

    def expected(_frozen, execution_id, directory, *, environ):
        return dict(
            candidate=_frozen["inputs"]["inventory"]["candidate"],
            execution_id=execution_id,
            run_id=123,
            job_id="matrix_windows_medium_g1",
            attempt=1,
        ), {}

    monkeypatch.setattr(m.runner, "checked", expected)
    monkeypatch.setattr(m.ingress, "accept_ci", lambda *a, **kw: (copy.deepcopy(frozen), {}))
    monkeypatch.setattr(
        m, "public_identity", lambda _: dict(recipient="fictional_public_recipient")
    )
    monkeypatch.setattr(m, "complete_handoff", lambda *a, **kw: None)
    rows = m.selected_rows(frozen, env)
    (target / "frozen-plan.json").write_bytes(m.launch.canonical(frozen))
    (target / "ready.json").write_bytes(
        m.launch.canonical(
            dict(
                format=1,
                admission_commit="c" * 40,
                plan_sha256=frozen["plan_sha256"],
                manifests=[
                    plan.digest(expected(frozen, row["execution_id"], None, environ=env)[0])
                    for row in rows
                ],
            )
        )
    )
    calls = []

    def execute(current, execution_id, directory, trial, *, environ):
        calls.append(("execute", execution_id))
        trial.mkdir()
        (trial / "process-state.json").write_text(
            json.dumps(
                dict(
                    format=1,
                    execution_id=execution_id,
                    state="driver_exited",
                    driver_exit_confirmed=True,
                    exit_code=0,
                )
            )
        )
        raw = trial / "trial/raw"
        raw.mkdir(parents=True)
        row = next(row for row in current["runs"] if row["execution_id"] == execution_id)
        (raw / "run-manifest.json").write_text(json.dumps(dict(result=row["expected_result"])))
        return dict(exit_code=0)

    def seal(raw, target, recipient, expected):
        calls.append(("seal", expected["candidate"]))
        target.mkdir()
        (target / "measurements.age").write_bytes(b"fictional sealed output")
        (target / "receipt.json").write_text("{}")

    def gate(target):
        return [str(target / name) for name in ("measurements.age", "receipt.json")]

    monkeypatch.setattr(m.transport, "validate_sealed", gate)
    return locals()


def test_wait_pending_then_fixed_b_never_calls_execution(module, monkeypatch, tmp_path):
    m = module
    q, b = "a" * 40, "b" * 40
    value = dict(tool_commit="c" * 40)
    monkeypatch.setattr(m, "probe_request", lambda *a, **kw: (q, dict(plan_commit="d" * 40)))
    monkeypatch.setattr(m.ingress, "pair", lambda *a: (value, b"", b""))
    monkeypatch.setattr(m, "tool_snapshot", lambda *a: None)
    monkeypatch.setattr(m, "binding", lambda *a: (value, dict(request_commit=q)))

    def forbidden(*a, **kw):
        raise AssertionError("measurement work reached during wait")

    monkeypatch.setattr(m.runner, "launch_one", forbidden)
    monkeypatch.setattr(m.versions, "prepare", forbidden)
    monkeypatch.setattr(m.matrix, "register_plan", forbidden)
    observed = iter((q, b))
    slept = []
    result = m.wait_for_admission(
        tmp_path,
        {},
        {},
        repository=lambda: dict(full_name=m.transport.REPOSITORY, private=True),
        fetch=lambda *a: next(observed),
        sleep=slept.append,
    )
    assert result == dict(
        eligible="true", admission_commit=b, request_commit=q, tool_commit="c" * 40
    )
    assert slept == [m.POLL_SECONDS]


@pytest.mark.parametrize("mode", ["timeout", "network", "unknown", "skip"])
def test_wait_has_no_retry_on_network_or_unknown_admission(module, monkeypatch, tmp_path, mode):
    m = module
    q = "a" * 40
    monkeypatch.setattr(
        m, "probe_request", lambda *a, **kw: None if mode == "skip" else (q, dict(plan_commit=q))
    )
    monkeypatch.setattr(m.ingress, "pair", lambda *a: ({}, b"", b""))
    monkeypatch.setattr(m, "tool_snapshot", lambda *a: None)
    counts = []

    def fetch(*a):
        counts.append(1)
        if mode == "network":
            raise ValueError("fictional network failure")
        return q if mode == "timeout" else "b" * 40

    monkeypatch.setattr(
        m, "binding", lambda *a: (_ for _ in ()).throw(ValueError("unknown admission"))
    )
    if mode == "skip":
        assert m.wait_for_admission(tmp_path, {}, {}, fetch=fetch)["eligible"] == "false"
        assert not counts
    else:
        with pytest.raises(ValueError):
            m.wait_for_admission(tmp_path, {}, {}, fetch=fetch, sleep=lambda _: None)
        assert len(counts) == (m.POLLS if mode == "timeout" else 1)


def test_network_requires_explicit_executor_and_token_stays_out_of_argv(
    module, monkeypatch, tmp_path
):
    m = module
    with pytest.raises(ValueError, match="network_executor_not_supplied"):
        m.fetch_admission(tmp_path, dict(GH_TOKEN="fictional_token"))
    seen = []
    monkeypatch.setattr(m.versions, "_git", lambda *a: b"a" * 40)

    def execute(argv, **kw):
        seen.append((argv, kw))
        return SimpleNamespace(returncode=0)

    assert (
        m.fetch_admission(tmp_path, dict(GH_TOKEN="fictional_token"), execute=execute) == "a" * 40
    )
    argv, kw = seen[0]
    assert type(argv) is list and all("fictional_token" not in item for item in argv)
    assert "GH_TOKEN" not in kw["env"] and kw["timeout"] == m.NETWORK_SECONDS
    assert "GIT_CONFIG_VALUE_1" in kw["env"]


@pytest.mark.parametrize("public", [False, True])
def test_slots_follow_frozen_order_and_publish_exact_cipher_pair(setup, public):
    s = setup
    m = s["m"]
    s["repository"] = lambda: dict(full_name=m.transport.REPOSITORY, private=not public)
    for ordinal in range(4):
        result = m.run_slot(
            s["tmp_path"],
            ordinal,
            s["env"],
            {},
            repository=s["repository"],
            execute=s["execute"],
            seal=s["seal"],
        )
        assert result["succeeded"] and len(result["paths"]) == 2
        assert [Path(path).name for path in result["paths"]] == ["measurements.age", "receipt.json"]
    assert [value for kind, value in s["calls"] if kind == "execute"] == [
        row["execution_id"] for row in s["rows"]
    ]
    before = list(s["calls"])
    with pytest.raises(FileExistsError):
        m.run_slot(
            s["tmp_path"],
            0,
            s["env"],
            {},
            repository=s["repository"],
            execute=s["execute"],
            seal=s["seal"],
        )
    assert s["calls"] == before


@pytest.mark.parametrize("failure", ["exit_code", "unknown_exit", "seal", "private", "material"])
def test_failed_raw_preserved_without_retry_or_plaintext_upload(setup, failure):
    s = setup
    m = s["m"]
    execute, seal, repository = s["execute"], s["seal"], s["repository"]

    def run(*args, **kw):
        result = execute(*args, **kw)
        if failure == "exit_code":
            state_path = args[3] / "process-state.json"
            state = json.loads(state_path.read_text())
            state["exit_code"] = 1
            state_path.write_text(json.dumps(state))
            return dict(exit_code=1)
        if failure == "unknown_exit":
            (args[3] / "process-state.json").write_text(
                json.dumps(dict(state="child_exit_unconfirmed", driver_exit_confirmed=False))
            )
            raise KeyboardInterrupt()
        if failure == "material":
            s["monkeypatch"].setattr(
                m.runner, "checked", lambda *a, **kw: (_ for _ in ()).throw(ValueError("changed"))
            )
        return result

    if failure == "seal":

        def seal(*a):
            raise ValueError("failed seal")

    if failure == "private":
        observations = iter((True, None))

        def repository():
            return dict(full_name=m.transport.REPOSITORY, private=next(observations))

    result = m.run_slot(
        s["tmp_path"], 0, s["env"], {}, repository=repository, execute=run, seal=seal
    )
    assert not result["succeeded"]
    assert bool(result["paths"]) == (failure == "exit_code")
    status = result["outcome"]
    assert (
        status["failure_stage"]
        == {
            "exit_code": "execute",
            "unknown_exit": "exit_check",
            "seal": "seal",
            "private": "upload_gate",
            "material": "material_check",
        }[failure]
    )
    assert status["driver_exit_confirmed"] == (failure != "unknown_exit")
    assert status["ciphertext_saved"] == (failure in {"exit_code", "private"})
    assert status["worker_exit"] == "not_evaluated_here"
    assert status["artifact_upload"] == "not_observed" and status["accepted"] is False
    assert (s["target"] / "slot-0/trial/raw/run-manifest.json").exists()
    assert sum(kind == "execute" for kind, _ in s["calls"]) == 1
    with pytest.raises(ValueError):
        m.run_slot(
            s["tmp_path"],
            1,
            s["env"],
            {},
            repository=s["repository"],
            execute=execute,
            seal=s["seal"],
        )


@pytest.mark.parametrize("damage", ["ready", "plan", "slot_order", "identity"])
def test_no_formal_entry_before_all_preconditions(setup, damage):
    s = setup
    m = s["m"]
    if damage == "ready":
        (s["target"] / "ready.json").write_text("{}")
    elif damage == "plan":
        (s["target"] / "frozen-plan.json").write_text("{}")
    elif damage == "identity":
        s["monkeypatch"].setattr(
            m.ingress, "accept_ci", lambda *a, **kw: (_ for _ in ()).throw(ValueError("rejected"))
        )
    with pytest.raises((ValueError, OSError)):
        m.run_slot(
            s["tmp_path"],
            1 if damage == "slot_order" else 0,
            s["env"],
            {},
            repository=s["repository"],
            execute=s["execute"],
            seal=s["seal"],
        )
    assert not s["calls"]


def test_output_never_accepts_plaintext_path_by_plan_field(module, tmp_path):
    path = tmp_path / "output.txt"
    path.write_text("")
    with pytest.raises(ValueError):
        module.output(dict(GITHUB_OUTPUT=str(path)), dict(raw="fictional.json"))
    assert not path.read_text()


@pytest.fixture
def git_case(module, tmp_path, monkeypatch):
    value = ingress_tests.setup.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "ingress", value["m"])
    value["event"]["pull_request"]["head"]["ref"] = module.BRANCH
    return value


def test_all_twenty_four_groups_are_serial_and_upload_only_the_pair():
    path = Path(__file__).resolve().parents[1] / ".github/workflows/measurement-matrix.yml"
    workflow = yaml.safe_load(path.read_text())
    assert workflow["on"] == {
        "pull_request": {"types": ["synchronize"], "paths": [".github/a43-matrix-request.json"]}
    }
    assert workflow["permissions"] == {"contents": "read"}
    m = load_tool("measurement_matrix_workflow")
    expected = list(m.matrix.jobs(frozen_plan()).values())
    assert list(workflow["jobs"]) == expected and len(expected) == 24
    previous = None
    for name, job in workflow["jobs"].items():
        assert job.get("needs") == previous
        previous = name
        slots = [step for step in job["steps"] if step.get("id", "").startswith("slot")]
        assert [step["id"] for step in slots] == ["slot0", "slot1", "slot2", "slot3"]
        for step in slots:
            assert "success()" in step["if"]
        uploads = [
            step for step in job["steps"] if "actions/upload-artifact@" in step.get("uses", "")
        ]
        assert len(uploads) == 4
        confirms = [step for step in job["steps"] if " confirm-upload " in step.get("run", "")]
        assert len(confirms) == 4
        for index, confirm in enumerate(confirms):
            assert "success()" in confirm["if"]
            assert (
                confirm["env"]["ARTIFACT_ID"] == "${{ steps.upload%d.outputs.artifact-id }}" % index
            )
            assert job["steps"].index(uploads[index]) < job["steps"].index(confirm)
            if index < 3:
                assert job["steps"].index(confirm) < job["steps"].index(slots[index + 1])
        for index, step in enumerate(uploads):
            assert step["with"]["path"].splitlines() == [
                "${{ steps.slot%d.outputs.ciphertext }}" % index,
                "${{ steps.slot%d.outputs.receipt }}" % index,
            ]
            assert step["with"]["if-no-files-found"] == "error"


@pytest.mark.parametrize(
    "damage", [None, "missing_entered", "unknown_exit", "failed_exit", "changed_manifest", "sealed"]
)
def test_production_handoff_enables_only_confirmed_arm(tmp_path, monkeypatch, damage):
    from tests.test_measurement_matrix_launch import setup as runner_setup

    m = load_tool("measurement_matrix_workflow")
    frozen, row, env = runner_setup(tmp_path, monkeypatch)
    directory = tmp_path / "sources"
    manifest, inputs = m.runner.checked(frozen, row["execution_id"], directory, environ=env)
    m.matrix.register_started(frozen, row["execution_id"], **inputs)
    if damage != "missing_entered":
        m.runner.claim_started(frozen, row["execution_id"], directory, environ=env)
    trial = tmp_path / "trial"
    raw = trial / "trial/raw"
    raw.mkdir(parents=True)
    (trial / "process-state.json").write_text(
        json.dumps(
            dict(
                format=1,
                execution_id=row["execution_id"],
                state="driver_exited",
                driver_exit_confirmed=damage != "unknown_exit",
                exit_code=1 if damage == "failed_exit" else 0,
            )
        )
    )
    (raw / "run-manifest.json").write_text(
        json.dumps(
            dict(
                manifest,
                result="incomplete" if damage == "changed_manifest" else row["expected_result"],
            )
        )
    )

    def validate(path):
        if damage == "sealed":
            raise ValueError("sealed invalid")
        return ["measurements.age", "receipt.json"]

    monkeypatch.setattr(m.transport, "validate_sealed", validate)
    monkeypatch.setattr(m.cipher_handoff, "confirm", lambda *a, **kw: validate(None))
    completed = m.matrix._registry() / (row["execution_id"] + ".completed.json")
    if damage:
        with pytest.raises((ValueError, OSError)):
            m.complete_handoff(
                frozen,
                row["execution_id"],
                directory,
                trial,
                tmp_path / "sealed",
                env,
                request_commit="a" * 40,
                artifact_id=123,
                artifact_digest="b" * 64,
            )
        assert not completed.exists()
    else:
        m.complete_handoff(
            frozen,
            row["execution_id"],
            directory,
            trial,
            tmp_path / "sealed",
            env,
            request_commit="a" * 40,
            artifact_id=123,
            artifact_digest="b" * 64,
        )
        assert completed.is_file()
        next_id = frozen["runs"][1]["execution_id"]
        m.matrix.register_started(frozen, next_id, **inputs)
        with pytest.raises(FileExistsError):
            m.complete_handoff(
                frozen,
                row["execution_id"],
                directory,
                trial,
                tmp_path / "sealed",
                env,
                request_commit="a" * 40,
                artifact_id=123,
                artifact_digest="b" * 64,
            )
