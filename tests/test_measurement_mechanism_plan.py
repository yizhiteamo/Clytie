"""Pure fictional plans and tiny fake files; never execute formal measurements."""

import copy
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from tests.measurement_tools import load
from tests.test_measurement_plan import inputs, profiles

plan = load("measurement_plan")
version = load("measurement_version")
launch = load("measurement_launch")
mechanism = load("measurement_mechanism_plan")


def materials():
    inventory, geometry = inputs()
    source = dict(
        format=1,
        mode="candidate",
        candidate=inventory["candidate"],
        source_commit=inventory["candidate"],
        archive_sha256="f" * 64,
        files={"src/clytie/__init__.py": "1" * 64},
    )
    files = dict.fromkeys(launch.TOOL_FILES, "2" * 64)
    tools = dict(files=files, sha256=plan.digest(files))
    runtime = {
        platform: dict(
            environment=dict(
                cpu_count=2, os=platform, release_sha256="3" * 64, machine_sha256="4" * 64
            ),
            python="3.12.1",
            sqlite="3.45.1",
        )
        for platform in plan.PLATFORMS
    }
    inventory.update(
        fixture_sha256=files["scripts/measurement_fixture.py"],
        tool_sha256=tools["sha256"],
        source_inventory_sha256=plan.digest(source),
        environments={key: plan.digest(value) for key, value in runtime.items()},
    )
    for item in geometry.values():
        item["fixture_sha256"] = inventory["fixture_sha256"]
    frozen = plan.freeze_plan(
        plan.draft_plan("mechanism", inventory, geometry, profiles=profiles())
    )
    workflow = dict(run_id=123, attempt=1, jobs=dict(Windows="windows_job", Linux="linux_job"))
    return frozen, source, tools, runtime, workflow


def arguments(data, index=0):
    frozen, source, tools, runtime, workflow = data
    row = frozen["runs"][index]
    return dict(
        source_inventory=source,
        tools=tools,
        runtime=runtime[row["platform"]],
        workflow=dict(
            run_id=workflow["run_id"],
            attempt=workflow["attempt"],
            job_id=workflow["jobs"][row["platform"]],
        ),
    )


@pytest.mark.parametrize("index", range(8))
def test_all_frozen_platform_scale_scenarios_derive_format_six_without_defaults(index):
    data = materials()
    frozen = data[0]
    row = frozen["runs"][index]
    saved = copy.deepcopy(data)
    manifest = mechanism.prepare_manifest(frozen, row["execution_id"], **arguments(data, index))
    assert manifest["format"] == 6
    assert manifest["phase"] == "mechanism"
    assert manifest["method"] == "legal_storage_v1"
    assert manifest["arm"] == "candidate"
    assert manifest["scenario"] == row["scenario"]
    assert manifest["result"] == "incomplete"
    assert manifest["observer_sequences"] == [0, 1]
    assert manifest["load"] == row["window"]["load"]
    assert manifest["parameters"] == plan.PARAMETERS
    assert manifest["execution"] == dict(order=["candidate"], position=0, warmup_requests=2)
    assert manifest["seed"] == 73
    assert "files" not in manifest and "cancel_trigger" not in manifest
    assert data == saved
    manifest["load"]["window_seconds"] = 999
    manifest["environment"]["cpu_count"] = 999
    assert data == saved


@pytest.mark.parametrize("field", ["seed", "parameters", "scale", "load", "warmup", "attempt"])
def test_altered_frozen_plan_is_rejected_even_if_outer_digest_is_recomputed(field):
    data = materials()
    frozen = data[0]
    row = frozen["runs"][0]
    if field == "seed":
        frozen["seed"] = 74
    elif field == "parameters":
        frozen["parameters"]["view_seconds"] = 600
    elif field == "scale":
        row["scale"] = "small_text"
    elif field == "load":
        row["window"]["load"]["maximum_seconds"] += 1
    elif field == "warmup":
        frozen["inputs"]["profiles"]["Windows"]["large_database"]["warmup_requests"] += 1
    else:
        row["attempt"] = 2
    frozen["plan_sha256"] = plan.digest({k: v for k, v in frozen.items() if k != "plan_sha256"})
    with pytest.raises(plan.PlanError):
        mechanism.prepare_manifest(frozen, row["execution_id"], **arguments(data))


@pytest.mark.parametrize(
    "mutation",
    [
        "source",
        "source_mode",
        "candidate",
        "tool",
        "fixture",
        "environment",
        "python",
        "sqlite",
        "platform",
        "retry",
    ],
)
def test_wrong_materials_runtime_or_workflow_are_rejected(mutation):
    data = materials()
    kwargs = arguments(data)
    if mutation == "source":
        kwargs["source_inventory"]["files"]["src/clytie/__init__.py"] = "9" * 64
    elif mutation == "source_mode":
        kwargs["source_inventory"]["mode"] = "original"
    elif mutation == "candidate":
        kwargs["source_inventory"]["candidate"] = "9" * 40
    elif mutation == "tool":
        kwargs["tools"]["files"]["scripts/measurement_plan.py"] = "9" * 64
    elif mutation == "fixture":
        kwargs["tools"]["files"]["scripts/measurement_fixture.py"] = "9" * 64
    elif mutation in {"python", "sqlite"}:
        kwargs["runtime"][mutation] = "9.9.9"
    elif mutation == "platform":
        kwargs["runtime"]["environment"]["os"] = "Linux"
    elif mutation == "environment":
        kwargs["runtime"]["environment"]["cpu_count"] += 1
    else:
        kwargs["workflow"]["attempt"] = 2
    with pytest.raises(ValueError):
        mechanism.prepare_manifest(data[0], data[0]["runs"][0]["execution_id"], **kwargs)


def test_rejects_missing_source_binding_unfrozen_matrix_unknown_id_and_arbitrary_manifest():
    data = materials()
    frozen = data[0]
    kwargs = arguments(data)
    entry = frozen["runs"][0]["execution_id"]
    with pytest.raises(mechanism.MechanismPlanError, match="execution_unknown"):
        mechanism.prepare_manifest(frozen, "0" * 64, **kwargs)
    with pytest.raises(TypeError):
        mechanism.prepare_manifest(frozen, entry, manifest={}, **kwargs)
    for kind, source_binding in (("mechanism", False), ("matrix", True)):
        inventory = copy.deepcopy(frozen["inputs"]["inventory"])
        if not source_binding:
            del inventory["source_inventory_sha256"]
        gate = (
            None
            if kind == "mechanism"
            else dict(
                plan_sha256="5" * 64,
                results_sha256="6" * 64,
                **{key: inventory[key] for key in ("candidate", "fixture_sha256", "tool_sha256")},
            )
        )
        draft = plan.draft_plan(
            kind, inventory, frozen["inputs"]["geometry"], profiles=profiles(), mechanism_pass=gate
        )
        with pytest.raises(plan.PlanError):
            mechanism.prepare_manifest(draft, draft["runs"][0]["execution_id"], **kwargs)
        other = plan.freeze_plan(draft)
        with pytest.raises(mechanism.MechanismPlanError):
            mechanism.prepare_manifest(other, other["runs"][0]["execution_id"], **kwargs)


def test_full_fakefile_source_and_tool_verification_detects_tampering(tmp_path):
    source = tmp_path / "version" / "source"
    source.mkdir(parents=True)
    (source / "fictional.txt").write_bytes(b"fictional")
    inventory = materials()[1]
    inventory["files"] = {"fictional.txt": hashlib.sha256(b"fictional").hexdigest()}
    (source.parent / "source-inventory.json").write_bytes(launch.canonical(inventory))
    tool_root = tmp_path / "tools"
    for name in launch.TOOL_FILES:
        path = tool_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fictional tool")
    verified, tools = mechanism.verify_materials(source.parent, tool_root)
    assert verified == inventory
    assert tools == launch.tool_inventory(tool_root)
    (source / "extra.txt").write_bytes(b"fictional extra")
    with pytest.raises(ValueError, match="source_changed"):
        mechanism.verify_materials(source.parent, tool_root)


def test_registry_uses_fixed_root_and_preserves_failure_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(mechanism, "ROOT", tmp_path)
    data = materials()
    frozen, _, _, _, workflow = data
    execution_id = frozen["runs"][0]["execution_id"]
    kwargs = arguments(data)
    with pytest.raises(FileNotFoundError):
        mechanism.register_started(frozen, execution_id, **kwargs)
    binding = mechanism.register_plan(frozen, workflow)
    assert mechanism.register_plan(frozen, workflow) == binding
    manifest = mechanism.register_started(frozen, execution_id, **kwargs)
    marker = tmp_path / ".cache/measurement-mechanism-admission" / (execution_id + ".started.json")
    before = marker.read_bytes()
    assert json.loads(before)["manifest_sha256"] == plan.digest(manifest)
    # A different output path cannot be supplied to gain another admission.
    with pytest.raises(TypeError):
        mechanism.register_started(frozen, execution_id, output=tmp_path / "new", **kwargs)
    with pytest.raises(mechanism.MechanismPlanError, match="already_started"):
        mechanism.register_started(frozen, execution_id, **kwargs)
    assert marker.read_bytes() == before


def test_partial_marker_and_fsync_failure_are_consumed_not_repaired(tmp_path, monkeypatch):
    monkeypatch.setattr(mechanism, "ROOT", tmp_path)
    data = materials()
    frozen = data[0]
    mechanism.register_plan(frozen, data[4])
    execution_id = frozen["runs"][0]["execution_id"]
    kwargs = arguments(data)
    original = mechanism.os.fsync

    def fail(_):
        raise OSError("fictional_write_failure")

    monkeypatch.setattr(mechanism.os, "fsync", fail)
    with pytest.raises(OSError, match="fictional_write_failure"):
        mechanism.register_started(frozen, execution_id, **kwargs)
    monkeypatch.setattr(mechanism.os, "fsync", original)
    with pytest.raises(mechanism.MechanismPlanError, match="already_started"):
        mechanism.register_started(frozen, execution_id, **kwargs)
    next_id = frozen["runs"][1]["execution_id"]
    marker = tmp_path / ".cache/measurement-mechanism-admission" / (next_id + ".started.json")
    marker.write_bytes(b"")
    with pytest.raises(mechanism.MechanismPlanError, match="already_started"):
        mechanism.register_started(frozen, next_id, **arguments(data, 1))
    assert marker.read_bytes() == b""


def test_concurrent_admission_has_exactly_one_winner(tmp_path, monkeypatch):
    monkeypatch.setattr(mechanism, "ROOT", tmp_path)
    data = materials()
    frozen = data[0]
    mechanism.register_plan(frozen, data[4])

    def register(_):
        try:
            mechanism.register_started(frozen, frozen["runs"][0]["execution_id"], **arguments(data))
            return "started"
        except mechanism.MechanismPlanError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(register, range(2)))
    assert sorted(outcomes) == ["mechanism_execution_already_started", "started"]


def test_changed_workflow_or_plan_binding_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(mechanism, "ROOT", tmp_path)
    data = materials()
    frozen = data[0]
    mechanism.register_plan(frozen, data[4])
    other = copy.deepcopy(data[4])
    other["run_id"] += 1
    with pytest.raises(mechanism.MechanismPlanError, match="registration_conflict"):
        mechanism.register_plan(frozen, other)
    kwargs = arguments(data)
    kwargs["workflow"]["run_id"] += 1
    with pytest.raises(mechanism.MechanismPlanError, match="workflow_mismatch"):
        mechanism.register_started(frozen, frozen["runs"][0]["execution_id"], **kwargs)
    altered = copy.deepcopy(frozen["inputs"])
    altered["inventory"]["verification_sha256"] = "7" * 64
    different_plan = plan.freeze_plan(plan.draft_plan(**altered))
    with pytest.raises(mechanism.MechanismPlanError, match="registration_conflict"):
        mechanism.register_plan(different_plan, data[4])
    assert not list((tmp_path / ".cache/measurement-mechanism-admission").glob("*.started.json"))


def test_all_eight_identities_consume_independently_and_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(mechanism, "ROOT", tmp_path)
    data = materials()
    frozen = data[0]
    mechanism.register_plan(frozen, data[4])
    for index, row in enumerate(frozen["runs"]):
        manifest = mechanism.register_started(frozen, row["execution_id"], **arguments(data, index))
        assert manifest["execution_id"] == row["execution_id"]
    for index, row in enumerate(frozen["runs"]):
        with pytest.raises(mechanism.MechanismPlanError, match="already_started"):
            mechanism.register_started(frozen, row["execution_id"], **arguments(data, index))
    assert (
        len(list((tmp_path / ".cache/measurement-mechanism-admission").glob("*.started.json"))) == 8
    )


@pytest.mark.parametrize("attempt", [True, 0, 2])
def test_invalid_attempt_is_rejected_before_registration(tmp_path, monkeypatch, attempt):
    monkeypatch.setattr(mechanism, "ROOT", tmp_path)
    data = materials()
    kwargs = arguments(data)
    kwargs["workflow"]["attempt"] = attempt
    with pytest.raises(mechanism.MechanismPlanError, match="retry_forbidden"):
        mechanism.prepare_manifest(data[0], data[0]["runs"][0]["execution_id"], **kwargs)
    workflow = copy.deepcopy(data[4])
    workflow["attempt"] = attempt
    with pytest.raises(mechanism.MechanismPlanError, match="retry_forbidden"):
        mechanism.register_plan(data[0], workflow)
    assert not (tmp_path / ".cache").exists()


def test_missing_other_platform_environment_is_not_synthesized():
    frozen = materials()[0]
    fields = copy.deepcopy(frozen["inputs"])
    del fields["inventory"]["environments"]["Linux"]
    with pytest.raises(plan.PlanError, match="plan_fields_invalid"):
        plan.draft_plan(**fields)


def test_source_inventory_hash_is_validated_and_changes_execution_ids():
    frozen = materials()[0]
    fields = copy.deepcopy(frozen["inputs"])
    fields["inventory"]["source_inventory_sha256"] = "8" * 64
    changed = plan.freeze_plan(plan.draft_plan(**fields))
    assert changed["runs"][0]["execution_id"] != frozen["runs"][0]["execution_id"]
    fields["inventory"]["source_inventory_sha256"] = "not_a_hash"
    with pytest.raises(plan.PlanError, match="plan_hash_invalid"):
        plan.draft_plan(**fields)


def test_damaged_plan_registration_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(mechanism, "ROOT", tmp_path)
    data = materials()
    mechanism.register_plan(data[0], data[4])
    path = tmp_path / ".cache/measurement-mechanism-admission/plan.json"
    path.write_bytes(b"{")
    with pytest.raises(ValueError):
        mechanism.register_plan(data[0], data[4])
    with pytest.raises(ValueError):
        mechanism.register_started(data[0], data[0]["runs"][0]["execution_id"], **arguments(data))
    assert path.read_bytes() == b"{"
