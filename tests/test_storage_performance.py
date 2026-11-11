"""存储性能判定只接受完整证据；边界用虚构缩小样本回归。"""

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import pytest


def script(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_evidence_cannot_be_performance_pass():
    module = script("evaluate_storage_performance")
    result = module.evaluate({}, {}, {})
    assert result["status"] == "incomplete"
    assert result["missing"]


def test_control_probe_requires_active_io_and_actual_cancel_settlement():
    module = script("evaluate_storage_performance")
    report = {"conditions": {"os": "Windows"}, "results": []}
    result = module.evaluate_control(report)
    assert result["status"] == "incomplete"


def evidence():
    module = script("evaluate_storage_performance")
    conditions = dict(
        runner_id="fictional_runner",
        os="Windows",
        machine="AMD64",
        release="fictional",
        processor="fictional",
        python="3.12.0",
        sqlite="3.45.0",
        logical_cpus=8,
        method_id=module.METHOD,
        commit="a" * 40,
        working_tree_modified=False,
    )
    old = {"conditions": {**conditions, "commit": module.OLD_COMMIT}, "results": []}
    new = {"conditions": conditions, "results": []}
    control = {"conditions": conditions, "results": []}
    zero = dict(ordinary=0, queued=0, finalizers=0, pending_views=0, capture_pending=False)
    for scale, parameters in module.SCALES.items():
        for repeat in range(1, 4):
            row = dict(
                scale=scale,
                scale_parameters=list(parameters),
                repeat=repeat,
                requests=2,
                interval_seconds=0.1,
                seed=123,
                with_backup=False,
                response_ms=module.percentiles([10.0, 10.0]),
                errors=[],
                samples=[
                    dict(
                        index=i,
                        scheduled_s=i * 0.1,
                        start_s=i * 0.1,
                        end_s=i * 0.1 + 0.01,
                        response_ms=10.0,
                        queue_delay_ms=0,
                        storage_queue_ms=0,
                        query_ms=0,
                        commit_ms=0,
                        model_ms=0,
                    )
                    for i in range(2)
                ],
            )
            old["results"].append(deepcopy(row))
            for backup in (False, True):
                item = deepcopy(row)
                item.update(
                    with_backup=backup,
                    order=(2 if backup else 1) if repeat % 2 else (1 if backup else 2),
                    committed_request_indices=[0, 1],
                    persisted_counter=1,
                    owner_status_at_end=zero.copy(),
                    owner_thread_alive_after_close=False,
                    snapshot_threads_alive_after_close=0,
                    resource_samples=[zero.copy(), zero.copy()],
                    peak_rss_bytes=100,
                    peak_temporary_bytes=100,
                    peak_wal_bytes=100,
                    loop_delays=[dict(at_s=0, delay_ms=0)],
                    stalled_stacks=[],
                    component_ms={
                        key: module.percentiles([0, 0])
                        for key in (
                            "queue_delay_ms",
                            "storage_queue_ms",
                            "query_ms",
                            "commit_ms",
                            "model_ms",
                        )
                    },
                    backup_start_s=0.001,
                    backup_end_s=0.101,
                    backup_load_coverage_seconds=0.1,
                    backup_load_coverage_ratio=1,
                    backup=dict(completeness="complete", missing=[], seconds=0.1),
                    capture_gate_hold_samples_ms=[1],
                    capture_gate_wait_samples_ms=[1],
                    phases=[dict(name="capture", start_s=0, end_s=1)],
                    capture_gate_hold_ms=module.percentiles([1]),
                    capture_gate_wait_ms=module.percentiles([1]),
                    capture_hold_exceeded_250ms=False,
                    capture_wait_exceeded_2000ms=False,
                )
                new["results"].append(item)
    for mode in ("fsync", "sqlite_commit"):
        for repeat in range(1, 4):
            control["results"].append(
                dict(
                    mode=mode,
                    repeat=repeat,
                    requested_samples=2,
                    errors=[],
                    queued_cancel_completed=True,
                    persisted_rows=1,
                    owner_thread_alive_after_close=False,
                    owner_status_at_end=zero.copy(),
                    injected_delay_seconds=1,
                    observed_io_block_seconds=1,
                    injection_method="owner_thread_event_wait_before_real_io",
                    status_ms=module.percentiles([1.0, 1.0]),
                    cancel_ms=module.percentiles([1.0, 1.0]),
                    samples=[
                        dict(
                            during_block=True,
                            cancel_accepted=True,
                            cancel_completed_at_return=False,
                            status_ms=1.0,
                            cancel_ms=1.0,
                        )
                        for _ in range(2)
                    ],
                )
            )
    return module, new, old, control


def test_complete_synthetic_evidence_passes_and_keeps_all_pairs():
    module, new, old, control = evidence()
    report = module.evaluate(new, old, control)
    assert report["status"] == "measured_thresholds_passed", report
    assert len(report["comparisons"]) == 12
    assert all(len(row["old_p99_values_ms"]) == 3 for row in report["comparisons"])
    assert "10.000" in module.render_summary(report)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("backup_load_coverage_ratio", 1 - 1e-6, "incomplete"),
        ("errors", [{"index": 0, "type": "StorageBusy"}], "failed"),
        ("persisted_counter", 0, "failed"),
        ("committed_request_indices", [0, 0], "failed"),
        ("owner_thread_alive_after_close", True, "incomplete"),
        ("backup", {"completeness": "partial", "missing": ["fixture"], "seconds": 1}, "failed"),
        ("capture_gate_hold_ms", {"count": 1, "max": 250.001}, "failed"),
        ("capture_gate_wait_ms", {"count": 1, "max": 2000.001}, "failed"),
    ],
)
def test_partial_or_bad_evidence_cannot_pass(field, value, expected):
    module, new, old, control = evidence()
    new["results"][1][field] = value
    assert module.evaluate(new, old, control)["status"] == expected


def set_latency(row, value):
    module = script("evaluate_storage_performance")
    for sample in row["samples"]:
        sample["response_ms"] = value
        sample["end_s"] = sample["start_s"] + value / 1000
    row["response_ms"] = module.percentiles([value] * len(row["samples"]))


def test_old_outlier_retained_and_cannot_relax_regression_threshold():
    module, new, old, control = evidence()
    set_latency(old["results"][1], 10_000)
    set_latency(old["results"][2], 20)
    set_latency(new["results"][0], 30.001)
    report = module.evaluate(new, old, control)
    assert report["status"] == "failed"
    comparison = report["comparisons"][0]
    assert comparison["old_p99_values_ms"] == [10, 10_000, 20]
    assert comparison["no_backup_limit_ms"] == 30


def test_backup_impact_uses_same_pair_and_unrounded_threshold():
    module, new, old, control = evidence()
    set_latency(new["results"][1], 110.0001)
    assert "small_text:1:backup_impact" in module.evaluate(new, old, control)["failures"]


@pytest.mark.parametrize("mutation", ["method", "runner", "repeat", "order", "raw", "queue"])
def test_comparability_and_series_are_required(mutation):
    module, new, old, control = evidence()
    if mutation == "method":
        old["conditions"]["method_id"] = "different_method"
    elif mutation == "runner":
        old["conditions"]["runner_id"] = "different_runner"
    elif mutation == "repeat":
        new["results"] = new["results"][:-2]
    elif mutation == "order":
        new["results"][0]["order"] = 2
    elif mutation == "raw":
        new["results"][0]["samples"] = []
    elif mutation == "queue":
        new["results"][0]["resource_samples"][-1]["queued"] = 1
    assert module.evaluate(new, old, control)["status"] != "measured_thresholds_passed"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status_ms", 100.001),
        ("cancel_ms", 250.001),
        ("cancel_completed_at_return", True),
        ("during_block", False),
    ],
)
def test_control_acceptance_has_independent_limits_and_is_not_completion(field, value):
    module, new, old, control = evidence()
    control["results"][0]["samples"][0][field] = value
    assert module.evaluate(new, old, control)["status"] != "measured_thresholds_passed"


def test_aggregate_requires_both_platforms_same_candidate():
    module, new, old, control = evidence()
    windows = module.evaluate(new, old, control)
    assert module.aggregate([windows])["status"] == "incomplete"
    linux = deepcopy(windows)
    linux["platform"] = "Linux"
    assert module.aggregate([windows, linux])["status"] == "measured_thresholds_passed"
    linux["candidate_commit"] = "b" * 40
    assert module.aggregate([windows, linux])["status"] == "incomplete"


def test_stall_evidence_stays_incomplete_without_arbitrary_override():
    module, new, old, control = evidence()
    new["results"][0]["stalled_stacks"] = [{"stack": [{"file": "fixture.py", "line": 1}]}]
    report = module.evaluate(new, old, control)
    assert report["status"] == "incomplete"
    assert any("business_stall_explanation_required" in item for item in report["missing"])


def test_reported_full_window_cannot_hide_short_raw_window():
    module, new, old, control = evidence()
    new["results"][1]["backup_end_s"] = 1
    assert module.evaluate(new, old, control)["status"] == "failed"


def test_raw_summary_conflict_cannot_be_overwritten_when_loading(tmp_path):
    module, new, old, control = evidence()
    row = new["results"][0]
    (tmp_path / "conditions.json").write_text(json.dumps(new["conditions"]))
    (tmp_path / "summary.json").write_text(json.dumps([row]))
    raw = deepcopy(row)
    raw["persisted_counter"] = 0
    case = tmp_path / "small_text-baseline-repeat-1"
    case.mkdir()
    (case / "result.json").write_text(json.dumps(raw))
    loaded = module.load_run(tmp_path)
    assert loaded["results"][0]["raw_summary_conflicts"] == ["persisted_counter"]
    assert module.evaluate(loaded, old, control)["status"] == "failed"


def test_merge_all_scale_directories_checks_conditions(tmp_path):
    module, new, old, control = evidence()
    directories = []
    for index, scale_names in enumerate((list(module.SCALES)[:3], list(module.SCALES)[3:])):
        directory = tmp_path / str(index)
        directory.mkdir()
        (directory / "conditions.json").write_text(json.dumps(new["conditions"]))
        rows = [row for row in new["results"] if row["scale"] in scale_names]
        (directory / "summary.json").write_text(json.dumps(rows))
        directories.append(directory)
    combined = module.load_runs(directories)
    assert module.evaluate(combined, old, control)["status"] == "measured_thresholds_passed"
    changed = {**new["conditions"], "runner_id": "other_runner"}
    (directories[1] / "conditions.json").write_text(json.dumps(changed))
    assert module.evaluate(module.load_runs(directories), old, control)["status"] == "incomplete"


@pytest.mark.asyncio
async def test_real_owner_probe_separates_acceptance_from_completion(tmp_path):
    module = script("benchmark_storage_control")
    for mode in ("fsync", "sqlite_commit"):
        result = await module.run_case(tmp_path / mode, mode, delay=0.15, samples=5, interval=0.01)
        assert result["errors"] == []
        assert result["queued_cancel_completed"] is True
        assert result["persisted_rows"] == 1
        assert result["owner_thread_alive_after_close"] is False
        assert result["observed_io_block_seconds"] >= 0.15
        assert all(
            sample["during_block"]
            and sample["cancel_accepted"]
            and not sample["cancel_completed_at_return"]
            for sample in result["samples"]
        )
