"""Fictional matrix runner handoffs only; never activate or execute product code."""

import json

import pytest
from tests.measurement_tools import load
from tests.test_measurement_matrix_plan import prepared
from tests.test_measurement_mechanism_launch import Process

driver = load("measurement_matrix_launch")


def setup(tmp_path, monkeypatch, index=0):
    frozen, sources, tools, runtime, _ = prepared()
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    monkeypatch.setattr(driver.matrix, "ROOT", tmp_path)
    row = frozen["runs"][index]
    jobs = driver.matrix.jobs(frozen)
    env = dict(
        GITHUB_ACTIONS="true",
        GITHUB_REPOSITORY=driver.transport.REPOSITORY,
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_RUN_ID="123",
        GITHUB_JOB=jobs[driver.matrix.group_key(row)],
    )
    monkeypatch.setattr(driver.platform, "system", lambda: row["platform"])
    monkeypatch.setattr(driver.preparation, "runtime", lambda: runtime[row["platform"]])
    monkeypatch.setattr(driver.preparation, "current", lambda *a: None)
    monkeypatch.setattr(driver, "verify_materials", lambda *a: (sources, tools))
    driver.matrix.register_plan(frozen, dict(run_id=123, attempt=1, jobs=jobs))
    return frozen, row, env


@pytest.mark.parametrize("index", range(0, 96, 4))
def test_each_group_uses_fixed_job_runtime_and_source(tmp_path, monkeypatch, index):
    frozen, row, env = setup(tmp_path, monkeypatch, index)
    manifest, _ = driver.checked(frozen, row["execution_id"], tmp_path / "sources", environ=env)
    assert manifest["source_commit"] == row["source_commit"]
    assert manifest["job_id"] == env["GITHUB_JOB"]
    env["GITHUB_JOB"] = "matrix_other"
    with pytest.raises(ValueError):
        driver.checked(frozen, row["execution_id"], tmp_path / "sources", environ=env)


def test_interruption_retains_unknown_and_consumes_slot(tmp_path, monkeypatch):
    frozen, row, env = setup(tmp_path, monkeypatch)
    target = tmp_path / "out"
    seen = []

    def spawn(argv, **kwargs):
        seen.append(argv)
        assert argv[1:4] == ["-B", "-m", "scripts.measurement_matrix_launch"]
        assert "CLYTIE_HOME" not in kwargs["env"]
        return Process(interrupt=True)

    with pytest.raises(KeyboardInterrupt):
        driver.launch_one(
            frozen, row["execution_id"], tmp_path / "sources", target, environ=env, spawn=spawn
        )
    state = json.loads((target / "process-state.json").read_bytes())
    assert state["driver_exit_confirmed"] is False
    with pytest.raises(ValueError, match="already_started"):
        driver.launch_one(
            frozen,
            row["execution_id"],
            tmp_path / "sources",
            tmp_path / "other",
            environ=env,
            spawn=spawn,
        )
    assert len(seen) == 1


@pytest.mark.parametrize("index", range(4))
def test_child_activates_selected_fixed_source_after_claim(tmp_path, monkeypatch, index):
    trial = load("measurement_trial")
    frozen, row, env = setup(tmp_path, monkeypatch, index)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    manifest, _ = driver.checked(frozen, row["execution_id"], tmp_path / "sources", environ=env)
    events = []
    monkeypatch.setattr(driver, "claim_started", lambda *a: events.append("claim") or manifest)
    selected = tmp_path / "sources" / driver.matrix.source_mode(row)

    def activate(directory):
        assert events == ["claim"] and directory == selected
        events.append("activate")
        return selected / "source", {}

    async def execute(directory, actual, product_root, *, tiny):
        assert tiny is False and actual == manifest and product_root == selected / "source"
        events.append("execute")
        return dict(manifest, result="pass")

    monkeypatch.setattr(driver.versions, "activate", activate)
    monkeypatch.setattr(trial, "verify_imports", lambda *a: events.append("verify"))
    monkeypatch.setattr(trial, "_execute", execute)
    coroutine = trial.run_matrix(
        tmp_path / "trial", frozen, row["execution_id"], tmp_path / "sources"
    )
    # The fake executor never suspends: no OS event loop/socket is needed.
    with pytest.raises(StopIteration) as stopped:
        coroutine.send(None)
    result = stopped.value.value
    assert result["result"] == "pass"
    assert events == ["claim", "activate", "execute", "verify"]


def test_verifier_requires_explicit_tool_object_and_all_three_archives(tmp_path, monkeypatch):
    frozen, sources, _, _, _ = prepared()
    inv = frozen["inputs"]["inventory"]
    inv["tool_commit"] = "c" * 40
    own = tmp_path / "tool.py"
    own.write_bytes(b"fictional tool bytes\n")
    path = "scripts/measurement_matrix_launch.py"
    tools = dict(files={path: driver.common.prep.digest(own.read_bytes())}, sha256="d" * 64)
    monkeypatch.setattr(driver, "__file__", str(own))
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    monkeypatch.setattr(driver.common.launch, "tool_inventory", lambda root: tools)
    calls = []
    monkeypatch.setattr(driver.preparation, "current", lambda root, commit: calls.append(commit))

    def git(root, *args):
        assert root == tmp_path and args == ("show", "c" * 40 + ":" + path)
        return own.read_bytes()

    def source_identity(root, directory, candidate, *, mode):
        assert root == tmp_path and directory == tmp_path / "sources" / mode
        assert candidate == inv["candidate"]
        calls.append(mode)
        return dict(
            source_inventory_sha256=driver.plan.digest(sources[mode]), tool_sha256=tools["sha256"]
        )

    monkeypatch.setattr(driver.versions, "_git", git)
    monkeypatch.setattr(driver.prep_transport, "fixed_source_identity", source_identity)
    monkeypatch.setattr(driver.versions, "verify", lambda directory: sources[directory.name])
    assert driver.verify_materials(frozen, tmp_path / "sources") == (sources, tools)
    assert calls == ["c" * 40, "candidate", "legacy", "original"]
    monkeypatch.setattr(driver.versions, "_git", lambda *a: b"other bytes")
    with pytest.raises(ValueError, match="tool_commit_mismatch"):
        driver.verify_materials(frozen, tmp_path / "sources")
    del inv["tool_commit"]
    with pytest.raises(ValueError, match="explicit_tool_commit_required"):
        driver.verify_materials(frozen, tmp_path / "sources")
