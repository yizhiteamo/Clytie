"""Local format-six numeric comparison; never a performance acceptance gate."""

import argparse
import hashlib
import math
import os
from pathlib import Path

from scripts import evaluate_storage_performance as legacy
from scripts import measurement_analysis_index as index
from scripts import measurement_evidence as evidence
from scripts import measurement_matrix_plan as matrix
from scripts import measurement_mechanism_plan as mechanism
from scripts import measurement_plan as plan


def require(value, code):
    if not value:
        raise ValueError(code)


def business_metric(records, load):
    rows = sorted((r for r in records if r["kind"] == "business"), key=lambda r: r["id"])
    require(len(rows) == load["request_count"] and bool(rows), "business_count")
    require([r["id"] for r in rows] == list(range(1, len(rows) + 1)), "business_ids")
    origin = rows[0]["scheduled_s"]
    for position, row in enumerate(rows):
        require(row["outcome"] == "ok", "business_outcome")
        require(
            all(legacy.finite(row[k]) for k in ("scheduled_s", "started_s", "finished_s"))
            and row["scheduled_s"] <= row["started_s"] <= row["finished_s"],
            "business_order",
        )
        require(
            math.isclose(
                row["scheduled_s"],
                origin + position * load["interval_seconds"],
                abs_tol=1e-6,
                rel_tol=0,
            ),
            "business_arrivals",
        )
    # Includes queue delay and backlog. No truncation at the arrival window.
    return legacy.percentiles([(r["finished_s"] - r["scheduled_s"]) * 1000 for r in rows])


def compare(rows):
    keys = {
        (p, s, g, a)
        for p in plan.PLATFORMS
        for s in plan.SCALES
        for g in (1, 2, 3)
        for a in ("baseline", "none", "original", "candidate")
    }
    by_key = {(r["platform"], r["scale"], r["group"], r["arm"]): r for r in rows}
    require(
        len(rows) == len(by_key) == 96 and set(by_key) == keys, "matrix_incomplete_or_duplicate"
    )
    comparisons, failures = [], []
    for platform in plan.PLATFORMS:
        for scale in plan.SCALES:
            old = [by_key[platform, scale, g, "baseline"]["business"]["p99"] for g in (1, 2, 3)]
            require(all(legacy.finite(v) for v in old), "baseline_metric")
            reference = min(old)  # Exact existing evaluator's conservative aggregation.
            for group in (1, 2, 3):
                values = {
                    a: by_key[platform, scale, group, a]["business"]["p99"]
                    for a in ("none", "original", "candidate")
                }
                require(all(legacy.finite(v) for v in values.values()), "matrix_metric")
                no_backup_limit = max(reference * 1.2, reference + 20)
                backup_limit = max(values["none"] * 2, values["none"] + 100)
                for failed, reason in (
                    (values["none"] > no_backup_limit, "no_backup_regression"),
                    (values["candidate"] > backup_limit, "backup_impact"),
                ):
                    if failed:
                        failures.append(f"{platform}:{scale}:{group}:{reason}")
                comparisons.append(
                    dict(
                        platform=platform,
                        scale=scale,
                        group=group,
                        old_p99_values_ms=old,
                        old_reference_ms=reference,
                        new_no_backup_p99_ms=values["none"],
                        original_p99_ms=values["original"],
                        backup_p99_ms=values["candidate"],
                        no_backup_limit_ms=no_backup_limit,
                        backup_limit_ms=backup_limit,
                    )
                )
    return dict(
        scope="matrix_numeric_only",
        status="not_evaluated",
        numeric_status="failed" if failures else "passed",
        failures=failures,
        comparisons=comparisons,
        missing=[
            "independent_resource_and_correctness_acceptance",
            "business_stall_explanation",
            "mechanism_acceptance",
        ],
        runs=rows,
    )


def read_json(path):
    with index.safe_stream(path) as stream:
        return evidence._json(stream.read())


def load_verified(directory, frozen, workflow_run_id):
    directory = evidence._safe_path(directory)
    state = read_json(directory / "state.json")
    require(
        state.get("accepted") is True
        and state.get("private_scan") == "pass"
        and state.get("state") == "verified_private_pass_generic_exception",
        "local_verification_missing",
    )
    raw = directory / "raw"
    require(set(state["files"]) == set(evidence.FILES), "verification_files")
    run = read_json(raw / "run-manifest.json")
    evidence.validate_run(run)
    require(run["format"] == 6, "format_six_required")
    row = matrix.select(frozen, run["execution_id"])
    runtime = {k: run[k] for k in ("environment", "python", "sqlite")}
    require(
        plan.digest(runtime) == frozen["inputs"]["inventory"]["environments"][row["platform"]],
        "runtime_mismatch",
    )
    expected = mechanism._manifest(
        frozen,
        row,
        runtime=runtime,
        workflow=dict(run_id=workflow_run_id, attempt=1, job_id="matrix_" + matrix.group_key(row)),
        phase="baseline" if row["arm"] == "baseline" else "matrix",
    )
    index.identity_matches(run, expected)
    require(run["result"] == state.get("measurement_result") == "pass", "run_not_pass")
    requests, resources, hashes = [], [], {}
    for name in evidence.FILES:
        digest = hashlib.sha256()
        limit = evidence.file_limit(name, 6)
        if name == "run-manifest.json":
            with index.safe_stream(raw / name, limit=limit) as stream:
                while block := stream.read(256 * 1024):
                    digest.update(block)
        else:
            for record in index.lines(raw / name, digest, limit=limit):
                evidence.validate_record(name, record, format_version=6)
                if name == "requests.jsonl":
                    requests.append(record)
                elif name == "resources.jsonl":
                    resources.append(record)
        actual = dict(size=(raw / name).stat().st_size, sha256=digest.hexdigest())
        require(actual == state["files"][name], "verified_raw_changed")
        hashes[name] = actual
    # Re-read manifest after hashing to reject identity changing between the two reads.
    require(read_json(raw / "run-manifest.json") == run, "manifest_changed")
    require(bool(resources), "resource_series_missing")
    business = business_metric(requests, run["load"])
    failures = [r for r in requests if r["outcome"] != "ok"]
    require(not failures, "request_outcome")
    if row["arm"] in {"candidate", "original"}:
        backup = [r for r in requests if r["kind"] == "backup"]
        require(len(backup) == 1, "backup_request_missing")
        business_rows = [r for r in requests if r["kind"] == "business"]
        require(
            min(r["scheduled_s"] for r in business_rows) <= backup[0]["started_s"]
            and max(r["finished_s"] for r in business_rows) >= backup[0]["finished_s"],
            "backup_window_uncovered",
        )
    return dict(
        platform=row["platform"],
        scale=row["scale"],
        group=row["group"],
        arm=row["arm"],
        execution_id=row["execution_id"],
        business=business,
        peaks={
            key: max(r[key] for r in resources)
            for key in ("rss_bytes", "wal_bytes", "temp_bytes", "queue", "views")
        },
        raw_files=hashes,
        ciphertext_sha256=state["ciphertext_sha256"],
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--verified-index", type=Path, required=True)
    parser.add_argument("--workflow-run-id", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    require(not os.environ.get("CI") and not os.environ.get("GITHUB_ACTIONS"), "local_only")
    require(args.workflow_run_id > 0, "workflow_run_id")
    frozen = read_json(args.plan)
    plan.validate_plan(frozen)
    paths = read_json(args.verified_index)
    require(
        type(paths) is list and len(paths) == 96 and all(type(p) is str for p in paths),
        "verified_index",
    )
    resolved = [evidence._safe_path(Path(p)).resolve() for p in paths]
    require(len(set(resolved)) == 96, "duplicate_verified_directory")
    for path in resolved:
        index.analysis_target(path, args.output)
    rows = [load_verified(path, frozen, args.workflow_run_id) for path in resolved]
    report = compare(rows)
    report.update(
        plan_sha256=frozen["plan_sha256"],
        workflow_run_id=args.workflow_run_id,
        verification_trust="existing_local_authenticated_decryption_state_not_reauthenticated",
    )
    output = evidence._new_directory(args.output)
    evidence._save(output / "numeric-analysis.json", report)
    return 1 if report["numeric_status"] == "failed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
