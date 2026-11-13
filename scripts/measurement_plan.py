"""Pure preparation of Issue #72 plans; never constructs data or executes trials.

The caller supplies inventories already checked against the fixed sources and
geometry exports from both platforms. Hash syntax is checked here, not the
truth of an external attestation. No source checkout, environment probing or
private file access happens in this module.

There is intentionally no default full-chain byte multiplier or time allowance.
Cost profiles must cite the inspected production boundaries, account for reads
and writes (including container framing/metadata estimates), and name every
reserve. Missing inputs produce an unfrozen draft. A frozen estimate is not a
runtime guarantee, a mechanism result, or permission to execute another round.
The trial adapter must enforce these plans separately; this module does not
relax the existing tiny-only trial guard or evidence manifest schema.
"""

import copy
import hashlib
import json
import math
import re
from decimal import ROUND_CEILING, Decimal

PLATFORMS = ("Windows", "Linux")
SCALES = {
    "small_text": (128, 8, 1024),
    "large_database": (32 * 1024, 8, 1024),
    "many_small_files": (128, 2048, 1024),
    "large_asset": (128, 1, 128 * 1024 * 1024),
}
LEDGER_TABLES = ("inbound", "record", "invocation", "intent", "attempt", "dedupe", "audit")
REFERENCES = {
    "baseline": "05ec93c7c4ae972b7475911b0aec9db57bb00390",
    "original": "b40bbbc31cfb2ff012a151d0e0e9c79acf07d903",
}
# Rotate the three comparison arms; alternate baseline position by group.
# This is one predeclared order, not a search over orders after seeing results.
ORDERS = (
    ("baseline", "none", "original", "candidate"),
    ("original", "candidate", "none", "baseline"),
    ("baseline", "candidate", "none", "original"),
)
PARAMETERS = {
    "concurrency": 1,
    "waiting": 8,
    "chunk_bytes": 256 * 1024,
    "normal_bytes_s": 16 * 1024 * 1024,
    "busy_bytes_s": 4 * 1024 * 1024,
    "burst_bytes": 512 * 1024,
    "signal_seconds": 0.1,
    "stale_seconds": 1,
    "view_seconds": 300,
    "wal_bytes": 256 * 1024 * 1024,
}
METRICS = (
    "memory_database_bytes",
    "ledger_database_bytes",
    "asset_file_bytes",
    "plugin_file_bytes",
)
RESERVES = ("startup", "native", "fsync", "release", "backlog")
REQUIRED_EVIDENCE = (
    "all_scheduled_business_outcomes_including_backlog",
    "control_response_latency",
    "single_instance_slot_across_real_executors",
    "actual_read_write_chunk_boundaries",
    "trusted_pressure_and_normal_busy_rates",
    "native_begin_driver_return_connection_close_stage_end",
    "native_internal_read_bytes_unknown",
    "view_generation_wal_limit_actual_view_release",
    "cancel_acceptance_and_actual_resource_release",
    "actual_executor_exit",
    "full_business_window_covers_native_backup_and_exit",
    "unchanged_raw_failures_and_no_automatic_retries",
)


class PlanError(ValueError):
    """Invalid input, incomplete preparation, or an altered frozen plan."""


def digest(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def _fields(value, fields):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise PlanError("plan_fields_invalid")


def _hash(value, length=64):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{%d}" % length, value) is None:
        raise PlanError("plan_hash_invalid")


def _number(value, *, integer=False, positive=False):
    if (
        type(value) not in ((int,) if integer else (int, float))
        or not math.isfinite(value)
        or value < 0
        or (positive and value == 0)
    ):
        raise PlanError("plan_number_invalid")


def _basis(value):
    if not isinstance(value, str) or not value.strip():
        raise PlanError("plan_basis_missing")


def _validate_inputs(inventory, geometry):
    # Legacy pure plans remain readable. Formal mechanism preparation requires
    # this complete canonical source-inventory digest; it is not a commit alias.
    source_fields = (
        ("source_inventory_sha256",)
        if isinstance(inventory, dict) and "source_inventory_sha256" in inventory
        else ()
    )
    tool_fields = (
        ("tool_commit",) if isinstance(inventory, dict) and "tool_commit" in inventory else ()
    )
    _fields(
        inventory,
        (
            "candidate",
            "fixture_sha256",
            "tool_sha256",
            "verification_sha256",
            "environments",
            *source_fields,
            *tool_fields,
        ),
    )
    _hash(inventory["candidate"], 40)
    for key in tool_fields:
        _hash(inventory[key], 40)
    for key in ("fixture_sha256", "tool_sha256", "verification_sha256"):
        _hash(inventory[key])
    for key in source_fields:
        _hash(inventory[key])
    _fields(inventory["environments"], PLATFORMS)
    _fields(geometry, PLATFORMS)
    for platform in PLATFORMS:
        _hash(inventory["environments"][platform])
        item = geometry[platform]
        _fields(
            item,
            ("format", "source", "purpose", "fixture_version", "fixture_sha256", "seed", "scales"),
        )
        if (
            type(item["format"]) is not int
            or item["format"] != 1
            or item["source"] != "generated_fictional_only"
            or item["purpose"] != "geometry_only_not_performance"
            or item["fixture_version"] != "legal_memory_v2"
            or item["fixture_sha256"] != inventory["fixture_sha256"]
            or type(item["seed"]) is not int
            or item["seed"] != 73
        ):
            raise PlanError("plan_geometry_invalid")
        _fields(item["scales"], SCALES)
        for scale, (records, files, size) in SCALES.items():
            metrics = item["scales"][scale]
            _fields(metrics, (*METRICS, "logical", "ledger_rows"))
            for key in METRICS:
                _number(metrics[key], integer=True)
            asset = scale == "large_asset"
            empty = dict.fromkeys(LEDGER_TABLES, 0)
            logical = dict(
                records=records,
                revisions=records,
                content_bytes=records * 4096,
                content_bytes_per_record=4096,
                content_length_distribution=dict(
                    min_bytes=4096, max_bytes=4096, total_bytes=records * 4096, records=records
                ),
                file_count=files,
                file_bytes=files * size,
                provider_files=0 if asset else files,
                provider_bytes=0 if asset else files * size,
                asset_registrations=files if asset else 0,
                asset_bytes=files * size if asset else 0,
                ledger_rows=empty,
            )
            # Canonical JSON also distinguishes bool from approved numeric zero/one.
            if (
                digest(metrics["logical"]) != digest(logical)
                or digest(metrics["ledger_rows"]) != digest(empty)
                or metrics["memory_database_bytes"] < records * 4096
                or metrics["ledger_database_bytes"] == 0
                or metrics["asset_file_bytes"] != logical["asset_bytes"]
                or metrics["plugin_file_bytes"] != logical["provider_bytes"]
            ):
                raise PlanError("plan_workload_changed")


def derive_window(geometry, profile):
    """Compute a conservative shaping estimate, not a measured physical-I/O bound.

    Each term is a geometry metric times explicit read/write passes. The supplied
    full-chain profile owns coverage of production stages; native internal bytes
    stay unknown. No initial burst discount is used. All timed estimates except
    backlog fall inside the business arrival window, including real exit. One
    extra arrival puts the last scheduled request no earlier than the estimate.
    """
    _fields(profile, ("basis", "phases", "reserves", "backup_start_seconds", "warmup_requests"))
    _basis(profile["basis"])
    _number(profile["backup_start_seconds"], positive=True)
    _number(profile["warmup_requests"], integer=True)
    _fields(profile["reserves"], RESERVES)
    reserves = {}
    for name, estimate in profile["reserves"].items():
        _fields(estimate, ("seconds", "basis"))
        _basis(estimate["basis"])
        _number(estimate["seconds"], positive=True)
        reserves[name] = Decimal(str(estimate["seconds"]))
    phases = profile["phases"]
    if not isinstance(phases, list) or not phases:
        raise PlanError("plan_phases_missing")
    names, costs = set(), []
    for phase in phases:
        _fields(phase, ("name", "basis", "terms", "estimated_extra_bytes"))
        _basis(phase["name"])
        _basis(phase["basis"])
        if phase["name"] in names or not isinstance(phase["terms"], list) or not phase["terms"]:
            raise PlanError("plan_phase_invalid")
        names.add(phase["name"])
        _number(phase["estimated_extra_bytes"], integer=True)
        reads = writes = 0
        for term in phase["terms"]:
            _fields(term, ("metric", "reads", "writes"))
            if term["metric"] not in METRICS or term["metric"] not in geometry:
                raise PlanError("plan_metric_invalid")
            _number(geometry[term["metric"]], integer=True)
            _number(term["reads"], integer=True)
            _number(term["writes"], integer=True)
            if term["reads"] + term["writes"] == 0:
                raise PlanError("plan_zero_cost_term")
            reads += geometry[term["metric"]] * term["reads"]
            writes += geometry[term["metric"]] * term["writes"]
        costs.append(
            dict(
                name=phase["name"],
                read_bytes=reads,
                write_bytes=writes,
                estimated_extra_bytes=phase["estimated_extra_bytes"],
            )
        )
    total = sum(
        row["read_bytes"] + row["write_bytes"] + row["estimated_extra_bytes"] for row in costs
    )
    normal = Decimal(total) / PARAMETERS["normal_bytes_s"]
    busy = Decimal(total) / PARAMETERS["busy_bytes_s"]
    finish = (
        Decimal(str(profile["backup_start_seconds"]))
        + busy
        + sum(reserves[key] for key in RESERVES if key != "backlog")
    )
    interval = Decimal("0.025")
    count = int((finish / interval).to_integral_value(rounding=ROUND_CEILING)) + 1
    window = count * interval
    return dict(
        phase_costs=costs,
        logical_io_bytes=total,
        normal_transfer_seconds=float(normal),
        busy_transfer_seconds=float(busy),
        estimated_backup_end_seconds=float(finish),
        native_internal_read_bytes=None,
        duration_is_guaranteed=False,
        burst_discount_bytes=0,
        view_seconds=PARAMETERS["view_seconds"],
        load=dict(
            window_seconds=float(window),
            interval_seconds=float(interval),
            request_count=count,
            maximum_seconds=float(window + reserves["backlog"]),
            backup_start_seconds=profile["backup_start_seconds"],
        ),
    )


def _window(metrics, profile):
    if profile is None:
        return None, ["cost_profile_missing"]
    missing = [
        key
        for key in ("basis", "phases", "reserves", "backup_start_seconds", "warmup_requests")
        if key not in profile
    ]
    if missing:
        return None, [key + "_missing" for key in missing]
    missing = ["reserve_" + key + "_missing" for key in RESERVES if key not in profile["reserves"]]
    if missing:
        return None, missing
    return derive_window(metrics, profile), []


def draft_plan(kind, inventory, geometry, *, profiles=None, mechanism_pass=None):
    """Return a reproducible draft; absent estimates/gate remain explicit gaps.

    Mechanism: one round, two scales/platform, normal and planned cancellation.
    Matrix: twelve legacy baselines plus thirty-six comparison runs/platform.
    No retry identities are allocated. A caller must retain all failed attempts.
    Matrix evidence hashes refer to the separately verified full mechanism pass;
    this function cannot turn schema-valid cancelled/raw archives into a pass.
    """
    if kind not in ("mechanism", "matrix"):
        raise PlanError("plan_kind_invalid")
    _validate_inputs(inventory, geometry)
    if profiles is None:
        profiles = {}
    if not isinstance(profiles, dict) or set(profiles) - set(PLATFORMS):
        raise PlanError("plan_profiles_invalid")
    for item in profiles.values():
        if not isinstance(item, dict) or set(item) - set(SCALES):
            raise PlanError("plan_profiles_invalid")
    if mechanism_pass is not None:
        if kind != "matrix":
            raise PlanError("plan_gate_invalid")
        _fields(
            mechanism_pass,
            ("plan_sha256", "results_sha256", "candidate", "fixture_sha256", "tool_sha256"),
        )
        for key, value in mechanism_pass.items():
            _hash(value, 40 if key == "candidate" else 64)
        if any(
            mechanism_pass[key] != inventory[key]
            for key in ("candidate", "fixture_sha256", "tool_sha256")
        ):
            raise PlanError("plan_gate_mismatch")
    inputs = copy.deepcopy(
        dict(
            kind=kind,
            inventory=inventory,
            geometry=geometry,
            profiles=profiles,
            mechanism_pass=mechanism_pass,
        )
    )
    namespace = digest(inputs)
    gaps = (
        ["mechanism_pass_evidence_missing"] if kind == "matrix" and mechanism_pass is None else []
    )
    runs = []
    selected = ("large_database", "large_asset") if kind == "mechanism" else tuple(SCALES)
    for platform in PLATFORMS:
        for scale in selected:
            window, missing = _window(
                geometry[platform]["scales"][scale], profiles.get(platform, {}).get(scale)
            )
            gaps.extend(f"{platform}/{scale}:{gap}" for gap in missing)
            groups = [(1, ("candidate",))] if kind == "mechanism" else enumerate(ORDERS, 1)
            for group, order in groups:
                for position, arm in enumerate(order):
                    scenarios = ("normal", "cancel") if kind == "mechanism" else ("normal",)
                    for scenario in scenarios:
                        identity = dict(
                            platform=platform, scale=scale, group=group, arm=arm, scenario=scenario
                        )
                        runs.append(
                            dict(
                                **identity,
                                execution_id=digest([namespace, identity]),
                                attempt=1,
                                order=list(order),
                                position=position,
                                source_commit=REFERENCES.get(arm, inventory["candidate"]),
                                expected_result="cancelled" if scenario == "cancel" else "pass",
                                cancel_trigger=(
                                    "first_asset_copy_chunk"
                                    if scale == "large_asset"
                                    else "first_budgeted_chunk_after_view_acquired"
                                )
                                if scenario == "cancel"
                                else None,
                                window=copy.deepcopy(window),
                            )
                        )
    return dict(
        format=1,
        purpose="measurement_plan_only",
        execution_scope="plan_only_not_execution_permission",
        status="unfrozen",
        inputs=inputs,
        references=copy.deepcopy(REFERENCES),
        fixture_version="legal_memory_v2",
        seed=73,
        parameters=copy.deepcopy(PARAMETERS),
        required_evidence=list(REQUIRED_EVIDENCE),
        gaps=gaps,
        runs=runs,
        failure_policy="retain_raw_stop_progression_no_retry_no_window_extension",
        unmet_trigger_policy="failed_not_replaced_or_retried",
    )


def _rebuild(plan):
    try:
        inputs = plan["inputs"]
        _fields(inputs, ("kind", "inventory", "geometry", "profiles", "mechanism_pass"))
        return draft_plan(**inputs)
    except (KeyError, TypeError) as exc:
        raise PlanError("plan_mismatch") from exc


def freeze_plan(plan):
    """Return an independently copied, hashed plan, refusing every unresolved gap."""
    expected = _rebuild(plan)
    if digest(plan) != digest(expected):
        raise PlanError("plan_mismatch")
    if expected["gaps"]:
        raise PlanError("plan_unfrozen:" + ",".join(expected["gaps"]))
    expected["status"] = "frozen"
    expected["plan_sha256"] = digest(expected)
    return expected


def validate_plan(plan):
    """Recompute schedule, budgets and digest; no mutable output is trusted."""
    expected = freeze_plan(_rebuild(plan))
    if digest(plan) != digest(expected):
        raise PlanError("plan_mismatch")
