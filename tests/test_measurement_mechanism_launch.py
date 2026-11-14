"""Fake executors and fictional admission records; no product or large fixture."""

import json
import sys

import pytest
from tests.measurement_tools import load
from tests.test_measurement_mechanism_plan import arguments, materials

draft = load("measurement_mechanism_launch")


def test_entrypoints():
    assert callable(getattr(draft, "launch_one", None))
    assert callable(getattr(draft, "claim_started", None))


def setup(tmp_path, monkeypatch, index=0):
    root = tmp_path / "repository"
    root.mkdir()
    monkeypatch.setattr(draft, "ROOT", root)
    monkeypatch.setattr(draft.mechanism, "ROOT", root)
    monkeypatch.setattr(
        draft.launch,
        "TOOL_FILES",
        draft.launch.TOOL_FILES,
    )
    data = materials()
    frozen, source, tools, runtime, workflow = data
    row = frozen["runs"][index]
    env = dict(
        GITHUB_ACTIONS="true",
        GITHUB_REPOSITORY=draft.transport.REPOSITORY,
        GITHUB_RUN_ID=str(workflow["run_id"]),
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_JOB=workflow["jobs"][row["platform"]],
    )
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(draft.platform, "system", lambda: row["platform"])
    monkeypatch.setattr(draft.preparation, "runtime", lambda: runtime[row["platform"]])
    monkeypatch.setattr(draft.preparation, "current", lambda *args: None)
    monkeypatch.setattr(draft.mechanism, "verify_materials", lambda *args: (source, tools))
    monkeypatch.setattr(
        draft.prep_transport,
        "material_identity",
        lambda *args: dict(
            source_inventory_sha256=draft.plan.digest(source), tool_sha256=tools["sha256"]
        ),
    )
    draft.mechanism.register_plan(frozen, workflow)
    return data, env, root / "version"


class Process:
    pid = 1234

    def __init__(self, code=0, interrupt=False):
        self.code, self.interrupt = code, interrupt

    def wait(self):
        if self.interrupt:
            raise KeyboardInterrupt
        return self.code

    def terminate(self):
        pytest.fail("Launcher must not terminate the child")

    kill = terminate


def fake_spawn(target, frozen, execution_id, *, code=0, raw=True, interrupt=False, calls=None):
    def spawn(argv, **kwargs):
        marker = draft.mechanism._registry() / (execution_id + ".started.json")
        assert marker.is_file()
        if calls is not None:
            calls.append(argv)
        assert argv[1:4] == ["-B", "-m", "scripts.measurement_mechanism_launch"]
        assert "--manifest" not in argv
        assert "CLYTIE_HOME" not in kwargs["env"]
        assert "GH_TOKEN" not in kwargs["env"]
        kwargs["stdout"].write(b"fictional child diagnostics\n")
        if raw:
            source = target / "trial" / "raw"
            source.mkdir(parents=True)
            expected = json.loads((target / "expected.json").read_bytes())
            for name in draft.evidence.FILES:
                value = dict(expected, result="fail") if name == "run-manifest.json" else {}
                (source / name).write_text(json.dumps(value))
        return Process(code, interrupt)

    return spawn


@pytest.mark.parametrize("index", range(8))
def test_all_eight_derive_without_changing_frozen_load(tmp_path, monkeypatch, index):
    data, env, version = setup(tmp_path, monkeypatch, index)
    frozen = data[0]
    row = frozen["runs"][index]
    target = tmp_path / "output"
    result = draft.launch_one(
        frozen,
        row["execution_id"],
        version,
        target,
        environ=env,
        spawn=fake_spawn(target, frozen, row["execution_id"], code=1),
    )
    expected = json.loads((target / "expected.json").read_bytes())
    assert expected["load"] == row["window"]["load"]
    assert expected["parameters"] == frozen["parameters"]
    assert result["driver_exit_confirmed"] is True
    assert result["raw_state"] == "present_requires_transport_validation"
    assert result["measurement_acceptance"] == "not_evaluated"
    assert (target / "child.log").read_bytes() == b"fictional child diagnostics\n"
    assert result["transport_validation"] == "not_run"


def test_wait_interruption_is_unknown_not_exit_and_cannot_retry(tmp_path, monkeypatch):
    data, env, version = setup(tmp_path, monkeypatch)
    frozen, execution_id = data[0], data[0]["runs"][0]["execution_id"]
    target = tmp_path / "output"
    calls = []
    spawn = fake_spawn(target, frozen, execution_id, interrupt=True, calls=calls)
    with pytest.raises(KeyboardInterrupt):
        draft.launch_one(frozen, execution_id, version, target, environ=env, spawn=spawn)
    state = json.loads((target / "process-state.json").read_bytes())
    assert state["driver_exit_confirmed"] is False
    assert state["state"] == "child_exit_unconfirmed"
    with pytest.raises(draft.mechanism.MechanismPlanError):
        draft.launch_one(
            frozen, execution_id, version, tmp_path / "retry", environ=env, spawn=spawn
        )
    assert len(calls) == 1


def test_missing_output_does_not_trigger_retry(tmp_path, monkeypatch):
    data, env, version = setup(tmp_path, monkeypatch)
    frozen, execution_id = data[0], data[0]["runs"][0]["execution_id"]
    target = tmp_path / "output"
    result = draft.launch_one(
        frozen,
        execution_id,
        version,
        target,
        environ=env,
        spawn=fake_spawn(target, frozen, execution_id, raw=False),
    )
    assert result["raw_state"] == "missing_or_invalid"
    assert result["driver_exit_confirmed"] is True
    with pytest.raises(draft.mechanism.MechanismPlanError):
        draft.launch_one(frozen, execution_id, version, tmp_path / "retry", environ=env)


def test_child_claim_rederives_manifest_and_is_one_use(tmp_path, monkeypatch):
    data, env, version = setup(tmp_path, monkeypatch)
    frozen, execution_id = data[0], data[0]["runs"][0]["execution_id"]
    original = draft.mechanism.register_started(frozen, execution_id, **arguments(data))
    assert draft.claim_started(frozen, execution_id, version, environ=env) == original
    with pytest.raises(FileExistsError):
        draft.claim_started(frozen, execution_id, version, environ=env)


def test_claim_requires_real_started_marker(tmp_path, monkeypatch):
    data, env, version = setup(tmp_path, monkeypatch)
    with pytest.raises((FileNotFoundError, draft.Error)):
        draft.claim_started(data[0], data[0]["runs"][0]["execution_id"], version, environ=env)


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_RUN_ID", "124"),
        ("GITHUB_JOB", "other_job"),
        ("GITHUB_REPOSITORY", "other/repository"),
    ],
)
def test_wrong_workflow_never_spawns(tmp_path, monkeypatch, field, value):
    data, env, version = setup(tmp_path, monkeypatch)
    env[field] = value

    def forbidden(*args, **kwargs):
        pytest.fail("Invalid workflow must never spawn")

    with pytest.raises(ValueError):
        draft.launch_one(
            data[0],
            data[0]["runs"][0]["execution_id"],
            version,
            tmp_path / "output",
            environ=env,
            spawn=forbidden,
        )


def test_spawn_failure_consumes_marker_and_keeps_failure(tmp_path, monkeypatch):
    data, env, version = setup(tmp_path, monkeypatch)
    frozen, execution_id = data[0], data[0]["runs"][0]["execution_id"]

    def fail(*args, **kwargs):
        raise OSError("fictional failure")

    target = tmp_path / "output"
    with pytest.raises(OSError):
        draft.launch_one(frozen, execution_id, version, target, environ=env, spawn=fail)
    assert json.loads((target / "process-state.json").read_bytes())["state"] == "spawn_unconfirmed"
    assert (draft.mechanism._registry() / (execution_id + ".started.json")).exists()


def trial_copy():
    return load("measurement_trial")


def complete(coroutine):
    # Fake trial bodies never suspend. No OS event loop/socket pair is required.
    try:
        coroutine.send(None)
    except StopIteration as done:
        return done.value
    finally:
        coroutine.close()
    pytest.fail("Pure fake coroutine unexpectedly suspended")


def test_new_trial_entry_claims_before_activation_and_uses_full_fixture_flag(tmp_path, monkeypatch):
    data, env, version = setup(tmp_path, monkeypatch)
    frozen, execution_id = data[0], data[0]["runs"][0]["execution_id"]
    expected = draft.mechanism.register_started(frozen, execution_id, **arguments(data))
    monkeypatch.setitem(sys.modules, "measurement_mechanism_launch", draft)
    trial = trial_copy()
    observed = []

    def activate(directory):
        assert (draft.mechanism._registry() / (execution_id + ".entered.json")).exists()
        observed.append("activated")
        return version / "source", data[1]

    monkeypatch.setattr(draft.mechanism.version, "activate", activate)
    monkeypatch.setattr(trial, "verify_imports", lambda path: None)

    async def execute(directory, manifest, source, *, tiny):
        observed.append(tiny)
        assert manifest == expected
        return dict(manifest, result="fail")

    monkeypatch.setattr(trial, "_execute", execute)
    result = complete(trial.run_mechanism(tmp_path / "trial", frozen, execution_id, version))
    assert observed == ["activated", False]
    assert result["result"] == "fail"
    with pytest.raises(FileExistsError):
        complete(trial.run_mechanism(tmp_path / "again", frozen, execution_id, version))


@pytest.mark.parametrize("arm", ["baseline", "none", "original", "candidate"])
def test_old_tiny_entry_stays_tiny(tmp_path, monkeypatch, arm):
    data, _, _ = setup(tmp_path, monkeypatch)
    trial = trial_copy()
    inventory = dict(
        data[1],
        source_commit={"baseline": draft.launch.LEGACY, "original": draft.launch.ORIGINAL}.get(
            arm, data[1]["candidate"]
        ),
    )
    manifest, _ = draft.launch.tiny_manifest(inventory, data[2], arm, "small_text")

    async def execute(directory, run, source, *, tiny):
        assert tiny is True
        return run

    monkeypatch.setattr(trial, "_execute", execute)
    assert complete(trial.run(tmp_path, manifest, tmp_path, tiny=True)) == manifest
    with pytest.raises(ValueError, match="launcher_not_frozen"):
        complete(trial.run(tmp_path, manifest, tmp_path, tiny=False))


def test_old_entry_cannot_accept_arbitrary_formal_manifest(tmp_path, monkeypatch):
    data, _, _ = setup(tmp_path, monkeypatch)
    manifest = draft.mechanism.prepare_manifest(
        data[0], data[0]["runs"][0]["execution_id"], **arguments(data)
    )
    trial = trial_copy()
    with pytest.raises(ValueError, match="launcher_not_frozen"):
        complete(trial.run(tmp_path, manifest, tmp_path, tiny=True))


def test_material_change_after_child_keeps_raw_and_denies_handoff(tmp_path, monkeypatch):
    data, env, version = setup(tmp_path, monkeypatch)
    frozen, execution_id = data[0], data[0]["runs"][0]["execution_id"]
    target = tmp_path / "output"
    original = fake_spawn(target, frozen, execution_id)

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        data[2]["sha256"] = "0" * 64
        return result

    with pytest.raises(ValueError):
        draft.launch_one(frozen, execution_id, version, target, environ=env, spawn=changed)
    assert (target / "trial/raw/run-manifest.json").exists()
    assert not (target / "handoff.json").exists()
    assert json.loads((target / "materials-after.json").read_bytes())["matched"] is False


def test_output_setup_failure_does_not_unconsume_started(tmp_path, monkeypatch):
    data, env, version = setup(tmp_path, monkeypatch)
    frozen, execution_id = data[0], data[0]["runs"][0]["execution_id"]
    target = tmp_path / "existing"
    target.mkdir()
    with pytest.raises(FileExistsError):
        draft.launch_one(frozen, execution_id, version, target, environ=env)
    with pytest.raises(draft.mechanism.MechanismPlanError):
        draft.launch_one(frozen, execution_id, version, tmp_path / "new", environ=env)


def test_changed_started_marker_cannot_claim(tmp_path, monkeypatch):
    data, env, version = setup(tmp_path, monkeypatch)
    frozen, execution_id = data[0], data[0]["runs"][0]["execution_id"]
    draft.mechanism.register_started(frozen, execution_id, **arguments(data))
    marker = draft.mechanism._registry() / (execution_id + ".started.json")
    record = json.loads(marker.read_bytes())
    record["manifest_sha256"] = "0" * 64
    marker.write_bytes(draft.prep.canonical(record))
    with pytest.raises(draft.Error):
        draft.claim_started(frozen, execution_id, version, environ=env)
    assert not (draft.mechanism._registry() / (execution_id + ".entered.json")).exists()
