"""Check a planned cancel's source and lifecycle, never declare mechanism pass.

Archives preserve failed/incomplete rows. This additional predicate is explicit:
it neither rewrites their result nor replaces native/slot/pressure/WAL checks.
"""

import json

try:
    from . import measurement_evidence as evidence
    from .measurement_sample_validation import RequestPlan, ResourceReleases, SampleError
except ImportError:
    import measurement_evidence as evidence
    from measurement_sample_validation import RequestPlan, ResourceReleases, SampleError


def verify_cancel_evidence(contents, expected):
    evidence.validate_files(contents, expected)
    run = json.loads(contents["run-manifest.json"])
    if not (
        run["format"] == 6
        and run.get("scenario") == "cancel"
        and run["result"] == "cancelled"
        and run["arm"] == "candidate"
        and run["scale"] in {"large_database", "large_asset"}
    ):
        raise SampleError("measurement_planned_cancel_required")
    plan = RequestPlan(run, expected_backup="cancelled")
    for line in evidence._lines(contents["requests.jsonl"]):
        plan.add(json.loads(line))
    plan.complete()
    backup = plan.backups[0]
    if backup["finished_s"] > plan.start + run["load"]["window_seconds"]:
        raise SampleError("measurement_backup_window_uncovered")
    cancellations = []
    for line in evidence._lines(contents["storage-phases.jsonl"]):
        row = json.loads(line)
        if row["phase"] == "planned_cancel":
            cancellations.append(row)
            if len(cancellations) > 4:
                raise SampleError("measurement_cancel_lifecycle_missing")
    if [row["event"] for row in cancellations] != [
        "triggered",
        "requested",
        "accepted",
        "terminal",
    ]:
        raise SampleError("measurement_cancel_lifecycle_missing")
    trigger, requested, accepted, terminal = cancellations
    binding = ("trigger", "source_row_id", "source_at_s", "stage_id", "view_id")
    if any(any(row[key] != trigger[key] for key in binding) for row in cancellations):
        raise SampleError("measurement_cancel_binding_changed")
    source = None
    for line in evidence._lines(contents["storage-phases.jsonl"]):
        row = json.loads(line)
        if row["id"] == trigger["source_row_id"]:
            if source is not None:
                raise SampleError("measurement_cancel_source_missing")
            source = row
    if source is None:
        raise SampleError("measurement_cancel_source_missing")
    if (
        source.get("stage_id") != trigger["stage_id"]
        or source["at_s"] != trigger["source_at_s"]
        or source.get("outcome") != "ok"
        or source.get("process_sequence") != 0
    ):
        raise SampleError("measurement_cancel_source_mismatch")
    database = run["scale"] == "large_database"
    if database:
        matches = (
            trigger["trigger"] == "first_budgeted_chunk_after_view_acquired"
            and source["phase"] == "sqlite_batch"
            and source["event"] == "progress"
            and (source.get("copied_pages") or 0) > 0
        )
    else:
        matches = (
            trigger["trigger"] == "first_asset_copy_chunk"
            and source["phase"] == "file_batch"
            and source["event"] == "finished"
            and source.get("direction") == "write"
            and (source.get("actual_bytes") or 0) > 0
        )
    if not matches:
        raise SampleError("measurement_cancel_trigger_invalid")
    if not (
        backup["started_s"]
        <= source["at_s"]
        <= trigger["at_s"]
        <= requested["at_s"]
        <= accepted["at_s"]
        <= terminal["at_s"]
        <= backup["finished_s"]
        and terminal["terminal_phase"] == "cancelled"
        and terminal["cancel_requested"] is True
    ):
        raise SampleError("measurement_cancel_timing_or_terminal_invalid")
    resources = ResourceReleases(run)
    acquired_views = 0
    for line in evidence._lines(contents["release-events.jsonl"]):
        row = json.loads(line)
        resources.add(row)
        if (
            database
            and row["resource"] == "view"
            and row["event"] == "acquired"
            and row["id"] == trigger["view_id"]
            and row["at_s"] <= source["at_s"]
        ):
            acquired_views += 1
    resources.complete()
    if database and acquired_views != 1:
        raise SampleError("measurement_cancel_view_missing")
    return dict(
        planned_cancel_verified=True,
        business_schedule_verified=True,
        resource_events_verified=True,
        mechanism_acceptance="not_evaluated",
    )
