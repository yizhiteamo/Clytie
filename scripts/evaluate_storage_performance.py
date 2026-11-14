"""按 storage-spec 固定门槛核对原始性能证据；不豁免异常，不代替功能验收。

单平台：--candidate DIR --baseline DIR --control DIR --output NEW_DIR
双平台：--aggregate windows/evaluation.json linux/evaluation.json --output NEW_DIR
"""

import argparse
import json
import math
import re
from pathlib import Path

SCALES = {
    "small_text": (128, 8, 1024),
    "large_database": (32 * 1024, 8, 1024),
    "many_small_files": (128, 2048, 1024),
    "large_asset": (128, 1, 128 * 1024 * 1024),
}
METHOD = "fixed-query-coordinated-meta-fake-model-v1"
OLD_COMMIT = "05ec93c7c4ae972b7475911b0aec9db57bb00390"


def finite(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def percentiles(values):
    if not values or any(not finite(value) for value in values):
        return None
    ordered = sorted(values)
    return {
        "count": len(values),
        **{f"p{p}": ordered[math.ceil(len(values) * p / 100) - 1] for p in (50, 95, 99)},
        "max": ordered[-1],
    }


def verdict(failures, missing):
    return "failed" if failures else "incomplete" if missing else "measured_thresholds_passed"


def evaluate_control(control):
    failures, missing, groups = [], [], []
    rows = control.get("results", [])
    for mode in ("fsync", "sqlite_commit"):
        selected = [row for row in rows if row.get("mode") == mode]
        if len(selected) < 3:
            missing.append(f"control:{mode}:three_repeats")
        indices = [row.get("repeat") for row in selected]
        if indices != list(range(1, len(selected) + 1)):
            missing.append(f"control:{mode}:repeat_sequence")
        samples = []
        for row in selected:
            label = f"control:{mode}:{row.get('repeat')}"
            actual = row.get("samples")
            if not actual or len(actual) != row.get("requested_samples"):
                missing.append(label + ":samples")
                continue
            samples.extend(actual)
            if row.get("errors") != []:
                failures.append(label + ":errors")
            if row.get("queued_cancel_completed") is not True or row.get("persisted_rows") != 1:
                failures.append(label + ":cancel_or_persistence")
            if row.get("owner_thread_alive_after_close") is not False:
                missing.append(label + ":owner_exit")
            status = row.get("owner_status_at_end", {})
            if any(status.get(key) != 0 for key in ("ordinary", "queued", "finalizers")):
                failures.append(label + ":unfinished_queue")
            if not finite(row.get("injected_delay_seconds")) or row["injected_delay_seconds"] <= 0:
                missing.append(label + ":injection")
            if row.get("injection_method") != "owner_thread_event_wait_before_real_io":
                missing.append(label + ":injection_method")
            if not finite(row.get("observed_io_block_seconds")):
                missing.append(label + ":observed_block")
            elif row["observed_io_block_seconds"] < row.get("injected_delay_seconds", 0):
                failures.append(label + ":injection_not_observed")
            if any(sample.get("during_block") is not True for sample in actual):
                missing.append(label + ":control_outside_block")
            if any(
                sample.get("cancel_accepted") is not True
                or sample.get("cancel_completed_at_return") is not False
                for sample in actual
            ):
                failures.append(label + ":acceptance_misreported_as_completion")
            for operation in ("status", "cancel"):
                metric = percentiles([sample.get(operation + "_ms") for sample in actual])
                if metric is None:
                    missing.append(label + ":" + operation + ":timings")
                elif metric["p99"] > 100 or metric["max"] > 250:
                    failures.append(label + ":" + operation + ":latency")
                if metric != row.get(operation + "_ms"):
                    failures.append(label + ":" + operation + ":raw_summary_mismatch")
        metrics = {}
        for operation in ("status", "cancel"):
            metric = percentiles([sample.get(operation + "_ms") for sample in samples])
            metrics[operation] = metric
            if metric is None:
                missing.append(f"control:{mode}:{operation}:timings")
            elif metric["p99"] > 100 or metric["max"] > 250:
                failures.append(f"control:{mode}:{operation}:latency")
        groups.append({"mode": mode, "metrics": metrics})
    return {
        "status": verdict(failures, missing),
        "failures": failures,
        "missing": missing,
        "groups": groups,
    }


def _sample_metric(row, label, failures, missing):
    samples = row.get("samples")
    requests = row.get("requests")
    if not samples or not isinstance(requests, int) or requests < 1 or len(samples) != requests:
        missing.append(label + ":raw_samples")
        return None
    if [sample.get("index") for sample in samples] != list(range(requests)):
        failures.append(label + ":sample_indices")
    if row.get("raw_summary_conflicts"):
        failures.append(label + ":raw_summary_conflicts")
    values = []
    for sample in samples:
        start, end, scheduled = (sample.get(key) for key in ("start_s", "end_s", "scheduled_s"))
        response = sample.get("response_ms")
        if not all(finite(value) for value in (start, end, scheduled, response)):
            missing.append(label + ":invalid_sample")
            return None
        if end < start or start < scheduled or response + 0.01 < (end - scheduled) * 1000:
            failures.append(label + ":sample_order")
        values.append(response)
    metric = percentiles(values)
    summary = row.get("response_ms")
    if not isinstance(summary, dict) or any(
        summary.get(key) != value for key, value in metric.items()
    ):
        failures.append(label + ":summary_raw_mismatch")
    return metric


def _resources(row, label, failures, missing):
    count = row.get("requests")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        missing.append(label + ":request_count")
        return
    committed = row.get("committed_request_indices")
    if not isinstance(committed, list):
        missing.append(label + ":commit_returns")
    elif committed != list(range(count)):
        failures.append(label + ":missing_or_duplicate_commit_return")
    if "persisted_counter" not in row:
        missing.append(label + ":persisted_counter")
    elif row["persisted_counter"] != count - 1:
        failures.append(label + ":persisted_counter")
    status = row.get("owner_status_at_end")
    if not isinstance(status, dict):
        missing.append(label + ":final_owner_status")
    elif (
        any(status.get(key) != 0 for key in ("ordinary", "queued", "finalizers"))
        or status.get("capture_pending") is not False
    ):
        failures.append(label + ":unfinished_resources")
    if row.get("owner_thread_alive_after_close") is not False:
        missing.append(label + ":owner_exit")
    if row.get("snapshot_threads_alive_after_close") != 0:
        missing.append(label + ":snapshot_exit")
    samples = row.get("resource_samples")
    if not isinstance(samples, list) or len(samples) < 2:
        missing.append(label + ":resource_series")
    else:
        for sample in samples:
            if (
                not all(
                    isinstance(sample.get(key), int) and sample[key] >= 0
                    for key in ("ordinary", "queued", "finalizers")
                )
                or type(sample.get("capture_pending")) is not bool
            ):
                missing.append(label + ":resource_series_fields")
                break
            if sample["ordinary"] > 64 or sample["queued"] > 80 or sample["finalizers"] > 16:
                failures.append(label + ":resource_bound")
        last = samples[-1]
        if (
            any(last.get(key) != 0 for key in ("ordinary", "queued", "finalizers"))
            or last.get("capture_pending") is not False
        ):
            failures.append(label + ":resource_series_not_drained")
    for key in ("peak_rss_bytes", "peak_temporary_bytes", "peak_wal_bytes"):
        if not finite(row.get(key)):
            missing.append(label + ":" + key)
    components = ("queue_delay_ms", "storage_queue_ms", "query_ms", "commit_ms", "model_ms")
    if not row.get("loop_delays") or not isinstance(row.get("component_ms"), dict):
        missing.append(label + ":loop_and_component_timings")
    else:
        for key in components:
            metric = percentiles([sample.get(key) for sample in row.get("samples", [])])
            if metric is None:
                missing.append(label + ":" + key)
            elif row["component_ms"].get(key) != metric:
                failures.append(label + ":component_summary_mismatch:" + key)


def _backup_window(row, label, failures, missing):
    start, end = row.get("backup_start_s"), row.get("backup_end_s")
    samples = row.get("samples", [])
    if not all(finite(value) for value in (start, end)) or not samples or end <= start:
        missing.append(label + ":backup_window_bounds")
        return
    first, last = samples[0].get("scheduled_s"), samples[-1].get("end_s")
    if not all(finite(value) for value in (first, last)):
        missing.append(label + ":backup_window_samples")
        return
    covered = max(0, min(last, end) - max(first, start))
    if first > start or last < end:
        missing.append(label + ":full_backup_window_raw")
    seconds = row.get("backup_load_coverage_seconds")
    if not finite(seconds) or not math.isclose(seconds, covered, abs_tol=1e-9):
        failures.append(label + ":coverage_summary_mismatch")
    duration = (row.get("backup") or {}).get("seconds")
    if not finite(duration) or not math.isclose(duration, end - start, abs_tol=1e-9):
        failures.append(label + ":backup_duration_mismatch")


def evaluate(candidate, baseline, control):
    failures, missing, comparisons = [], [], []
    new_conditions = candidate.get("conditions", {})
    old_conditions = baseline.get("conditions", {})
    platform = new_conditions.get("os")
    if platform not in {"Windows", "Linux"}:
        missing.append("platform")
    for key in (
        "runner_id",
        "os",
        "release",
        "machine",
        "processor",
        "python",
        "sqlite",
        "logical_cpus",
        "method_id",
    ):
        left, right = new_conditions.get(key), old_conditions.get(key)
        if left is None or right is None or left != right:
            missing.append("baseline_comparability:" + key)
    if new_conditions.get("method_id") != METHOD:
        missing.append("fixed_method")
    if old_conditions.get("commit") != OLD_COMMIT:
        missing.append("fixed_old_commit")
    if not re.fullmatch(r"[0-9a-f]{40}", str(new_conditions.get("commit", ""))):
        missing.append("candidate_commit")
    if new_conditions.get("working_tree_modified") is not False:
        missing.append("candidate_working_tree_modified_or_unknown")
    if candidate.get("condition_conflicts") or baseline.get("condition_conflicts"):
        missing.append("combined_run_conditions_conflict")
    for key in ("runner_id", "os"):
        if (
            control.get("conditions", {}).get(key) != new_conditions.get(key)
            or new_conditions.get(key) is None
        ):
            missing.append("control_comparability:" + key)
    if control.get("conditions", {}).get("commit") != new_conditions.get("commit"):
        missing.append("control_candidate_commit")
    control_result = evaluate_control(control)
    failures.extend(control_result["failures"])
    missing.extend(control_result["missing"])
    new_rows, old_rows = candidate.get("results", []), baseline.get("results", [])
    for scale, parameters in SCALES.items():
        new = [row for row in new_rows if row.get("scale") == scale]
        old = [
            row for row in old_rows if row.get("scale") == scale and row.get("with_backup") is False
        ]
        old_values = []
        for index, row in enumerate(old):
            label = f"old:{scale}:{index + 1}"
            metric = _sample_metric(row, label, failures, missing)
            if metric:
                old_values.append(metric["p99"])
            if row.get("errors") != []:
                missing.append(label + ":historical_errors")
        if len(old) < 3 or len(old_values) != len(old):
            missing.append(scale + ":old_three_repeats")
        if [row.get("repeat") for row in old] != list(range(1, len(old) + 1)):
            missing.append(scale + ":old_repeat_sequence")
        pairs = {}
        for row in new:
            repeat = row.get("repeat")
            backup = row.get("with_backup")
            label = f"new:{scale}:{repeat}:{backup}"
            if not isinstance(repeat, int) or repeat < 1 or type(backup) is not bool:
                missing.append(label + ":pair_identity")
                continue
            if backup in pairs.setdefault(repeat, {}):
                failures.append(label + ":duplicate_pair_member")
            pairs[repeat][backup] = row
            _sample_metric(row, label, failures, missing)
            if row.get("errors") != []:
                failures.append(label + ":errors")
            if tuple(row.get("scale_parameters", ())) != parameters:
                missing.append(label + ":full_scale")
            _resources(row, label, failures, missing)
            if "stalled_stacks" not in row:
                missing.append(label + ":business_stall_observation")
            elif row["stalled_stacks"]:
                missing.append(label + ":business_stall_explanation_required")
            if backup:
                _backup_window(row, label, failures, missing)
                if row.get("backup_load_coverage_ratio") != 1:
                    missing.append(label + ":full_backup_window")
                backup_result = row.get("backup") or {}
                if (
                    backup_result.get("completeness") != "complete"
                    or backup_result.get("missing") != []
                ):
                    failures.append(label + ":partial_backup")
                if not finite(backup_result.get("seconds")) or not row.get("phases"):
                    missing.append(label + ":backup_phases")
                for field, limit in (("capture_gate_hold_ms", 250), ("capture_gate_wait_ms", 2000)):
                    metric = row.get(field)
                    raw = percentiles(row.get(field.replace("_ms", "_samples_ms"), []))
                    if raw is None:
                        missing.append(label + ":" + field + ":raw")
                    elif raw != metric:
                        failures.append(label + ":" + field + ":raw_summary_mismatch")
                    if (
                        not isinstance(metric, dict)
                        or not finite(metric.get("max"))
                        or not metric.get("count")
                    ):
                        missing.append(label + ":" + field)
                    elif metric["max"] > limit:
                        failures.append(label + ":" + field)
                if (
                    row.get("capture_hold_exceeded_250ms") is not False
                    or row.get("capture_wait_exceeded_2000ms") is not False
                ):
                    failures.append(label + ":capture_breach_flag")
        if len(pairs) < 3 or sorted(pairs) != list(range(1, len(pairs) + 1)):
            missing.append(scale + ":three_contiguous_pairs")
        for repeat, pair in sorted(pairs.items()):
            if set(pair) != {False, True}:
                missing.append(f"{scale}:{repeat}:incomplete_pair")
                continue
            base, backup = pair[False], pair[True]
            expected = {False: 1, True: 2} if repeat % 2 else {True: 1, False: 2}
            if any(row.get("order") != expected[flag] for flag, row in pair.items()):
                missing.append(f"{scale}:{repeat}:alternating_order")
            for row in [backup, *old]:
                if any(
                    row.get(key) != base.get(key)
                    for key in ("scale_parameters", "requests", "interval_seconds", "seed")
                ):
                    missing.append(f"{scale}:{repeat}:load_mismatch")
            base_p99 = (base.get("response_ms") or {}).get("p99")
            backup_p99 = (backup.get("response_ms") or {}).get("p99")
            if not all(finite(value) for value in (base_p99, backup_p99)) or not old_values:
                continue
            reference = min(old_values)
            no_backup_limit = max(reference * 1.2, reference + 20)
            backup_limit = max(base_p99 * 2, base_p99 + 100)
            if base_p99 > no_backup_limit:
                failures.append(f"{scale}:{repeat}:no_backup_regression")
            if backup_p99 > backup_limit:
                failures.append(f"{scale}:{repeat}:backup_impact")
            comparisons.append(
                {
                    "scale": scale,
                    "repeat": repeat,
                    "old_p99_values_ms": old_values,
                    "old_reference_ms": reference,
                    "new_no_backup_p99_ms": base_p99,
                    "no_backup_limit_ms": no_backup_limit,
                    "backup_p99_ms": backup_p99,
                    "backup_limit_ms": backup_limit,
                }
            )
    return {
        "scope": "single_platform_performance",
        "platform": platform,
        "status": verdict(failures, missing),
        "failures": sorted(set(failures)),
        "missing": sorted(set(missing)),
        "comparisons": comparisons,
        "control": control_result,
        "candidate_commit": new_conditions.get("commit"),
        "limits": [
            "全部旧异常保留；最低旧P99只用于保守计算，不删异常或放宽门槛。",
            "提交序号是事务正常返回证据，末值是库核验；跨进程耐久、无重放及迟到收尾由功能证据另验。",
            "单平台通过不表示双平台性能或里程碑验收通过。",
        ],
    }


def aggregate(reports):
    failures, missing = [], []
    by_platform = {}
    for report in reports:
        platform = report.get("platform")
        if platform in by_platform:
            failures.append("duplicate_platform:" + str(platform))
        by_platform[platform] = report
        if report.get("scope") != "single_platform_performance":
            missing.append("invalid_platform_report")
        if report.get("status") == "failed":
            failures.append("platform_failed:" + str(platform))
        elif report.get("status") != "measured_thresholds_passed":
            missing.append("platform_incomplete:" + str(platform))
    if set(by_platform) != {"Windows", "Linux"}:
        missing.append("windows_and_linux_required")
    if len({row.get("candidate_commit") for row in reports}) != 1:
        missing.append("candidate_commit_mismatch")
    return {
        "scope": "dual_platform_performance",
        "status": verdict(failures, missing),
        "failures": failures,
        "missing": missing,
        "platforms": reports,
        "limits": ["本判定只覆盖性能测量；仍须固定候选的功能、强杀恢复、CI和双扫描证据。"],
    }


def load_run(directory):
    directory = Path(directory)
    conditions = json.loads((directory / "conditions.json").read_text(encoding="utf-8"))
    rows = json.loads((directory / "summary.json").read_text(encoding="utf-8"))
    for row in rows:
        if "scale" not in row:
            continue
        label = row["scale"] + ("-backup" if row["with_backup"] else "-baseline")
        repeat = row.get("repeat", 1)
        options = [
            directory / f"{label}-repeat-{repeat}" / "result.json",
            directory / label / "result.json",
        ]
        path = next((path for path in options if path.is_file()), None)
        if path is not None:
            raw = json.loads(path.read_text(encoding="utf-8"))
            conflicts = [
                key
                for key, value in raw.items()
                if key not in {"repeat", "order"} and key in row and row[key] != value
            ]
            if conflicts:
                row["raw_summary_conflicts"] = conflicts
            row.update({key: value for key, value in raw.items() if key not in row})
    return {"conditions": conditions, "results": rows}


def load_runs(directories):
    runs = [load_run(directory) for directory in directories]
    combined = {"conditions": runs[0]["conditions"], "results": [], "condition_conflicts": []}
    keys = (
        "runner_id",
        "os",
        "release",
        "machine",
        "processor",
        "python",
        "sqlite",
        "logical_cpus",
        "method_id",
        "commit",
        "working_tree_modified",
        "script_sha256",
    )
    for index, run in enumerate(runs):
        combined["results"].extend(run["results"])
        for key in keys:
            if run["conditions"].get(key) != combined["conditions"].get(key):
                combined["condition_conflicts"].append(f"run_{index + 1}:{key}")
    return combined


def render_summary(report):
    lines = [
        "# 存储性能判定",
        "",
        f"范围：{report['scope']}；结论：{report['status']}。",
        "显示数值保留三位小数；判定使用未舍入值。原始证据不覆盖、不删除。",
        "",
    ]
    for row in report.get("comparisons", []):
        old = ", ".join(f"{value:.3f}" for value in row["old_p99_values_ms"])
        lines.append(
            f"- {row['scale']} / 第 {row['repeat']} 组：旧P99 [{old}] ms；"
            f"新无备份 {row['new_no_backup_p99_ms']:.3f} / 门槛 {row['no_backup_limit_ms']:.3f} ms；"
            f"有备份 {row['backup_p99_ms']:.3f} / 门槛 {row['backup_limit_ms']:.3f} ms。"
        )
    for group in report.get("control", {}).get("groups", []):
        for op, metric in group["metrics"].items():
            value = (
                f"P99 {metric['p99']:.3f} ms；最大 {metric['max']:.3f} ms" if metric else "未测量"
            )
            lines.append(
                f"- control/{group['mode']}/{op}：{value}（门槛P99 100.000 ms，最大250.000 ms）。"
            )
    for key, title in (("failures", "不通过项"), ("missing", "证据缺项"), ("limits", "范围说明")):
        lines.extend(["", title + "：", *["- " + str(item) for item in report.get(key, [])]])
    for child in report.get("platforms", []):
        lines.extend(["", f"{child.get('platform')}：", render_summary(child)])
    return "\n".join(lines) + "\n"


def main(args):
    if args.aggregate:
        report = aggregate(
            [json.loads(Path(path).read_text(encoding="utf-8")) for path in args.aggregate]
        )
    else:
        report = evaluate(
            load_runs(args.candidate), load_runs(args.baseline), load_run(args.control)
        )
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=False)
    (output / "evaluation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output / "evaluation.md").write_text(render_summary(report), encoding="utf-8")
    print(f"performance evaluation: {report['status']} ({report['scope']})")
    return {"measured_thresholds_passed": 0, "failed": 1, "incomplete": 2}[report["status"]]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", nargs="+")
    parser.add_argument("--baseline", nargs="+")
    parser.add_argument("--control")
    parser.add_argument("--aggregate", nargs="+")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    if not args.aggregate and not all((args.candidate, args.baseline, args.control)):
        parser.error("candidate, baseline and control are required without aggregate")
    raise SystemExit(main(args))
