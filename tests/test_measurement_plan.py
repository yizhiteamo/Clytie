"""Plan arithmetic uses fictional metadata; no fixtures or trials are executed."""

import copy

import pytest
from tests.measurement_tools import load

plan = load("measurement_plan")


def inputs():
    inventory = {
        "candidate": "a" * 40,
        "fixture_sha256": "b" * 64,
        "tool_sha256": "c" * 64,
        "verification_sha256": "d" * 64,
        "environments": {platform: "e" * 64 for platform in plan.PLATFORMS},
    }
    geometry = {}
    for platform in plan.PLATFORMS:
        scales = {}
        for scale, (records, files, size) in plan.SCALES.items():
            asset = scale == "large_asset"
            ledger = dict.fromkeys(plan.LEDGER_TABLES, 0)
            scales[scale] = {
                "logical": {
                    "records": records,
                    "revisions": records,
                    "content_bytes": records * 4096,
                    "content_bytes_per_record": 4096,
                    "content_length_distribution": {
                        "min_bytes": 4096,
                        "max_bytes": 4096,
                        "total_bytes": records * 4096,
                        "records": records,
                    },
                    "file_count": files,
                    "file_bytes": files * size,
                    "provider_files": 0 if asset else files,
                    "provider_bytes": 0 if asset else files * size,
                    "asset_registrations": files if asset else 0,
                    "asset_bytes": files * size if asset else 0,
                    "ledger_rows": ledger,
                },
                "ledger_rows": ledger,
                "memory_database_bytes": records * 4096,
                "ledger_database_bytes": 4096,
                "asset_file_bytes": files * size if asset else 0,
                "plugin_file_bytes": 0 if asset else files * size,
            }
        geometry[platform] = {
            "format": 1,
            "source": "generated_fictional_only",
            "purpose": "geometry_only_not_performance",
            "fixture_version": "legal_memory_v2",
            "fixture_sha256": inventory["fixture_sha256"],
            "seed": 73,
            "scales": scales,
        }
    return inventory, geometry


def profile():
    # Small fictional arithmetic inputs, explicitly not a production cost model.
    return {
        "basis": "fictional complete-chain inventory for unit tests",
        "phases": [
            {
                "name": "fictional_copy",
                "basis": "fictional read and write boundaries",
                "terms": [{"metric": "memory_database_bytes", "reads": 1, "writes": 1}],
                "estimated_extra_bytes": 0,
            }
        ],
        "reserves": {
            name: {"seconds": seconds, "basis": "fictional estimate"}
            for name, seconds in dict(startup=1, native=2, fsync=3, release=4, backlog=5).items()
        },
        "backup_start_seconds": 0.1,
        "warmup_requests": 2,
    }


def profiles():
    return {platform: {scale: profile() for scale in plan.SCALES} for platform in plan.PLATFORMS}


def test_window_derives_from_bytes_and_named_estimates_without_claiming_guarantee():
    result = plan.derive_window({"memory_database_bytes": 1024}, profile())
    assert result["logical_io_bytes"] == 2048
    assert result["busy_transfer_seconds"] == 2048 / (4 * 1024 * 1024)
    assert result["normal_transfer_seconds"] == 2048 / (16 * 1024 * 1024)
    assert result["estimated_backup_end_seconds"] == pytest.approx(10.1 + 2048 / (4 * 1024**2))
    assert result["load"]["request_count"] == 406
    assert result["load"]["window_seconds"] == 10.15
    assert result["load"]["maximum_seconds"] == 15.15
    assert result["native_internal_read_bytes"] is None
    assert result["duration_is_guaranteed"] is False
    assert result["view_seconds"] == 300


def test_missing_costs_remain_visible_and_cannot_be_frozen():
    inventory, geometry = inputs()
    draft = plan.draft_plan("mechanism", inventory, geometry)
    assert draft["status"] == "unfrozen"
    assert len(draft["gaps"]) == 4
    assert all(run["window"] is None for run in draft["runs"])
    with pytest.raises(plan.PlanError, match="plan_unfrozen"):
        plan.freeze_plan(draft)


def test_mechanism_has_one_identity_per_preplanned_subscenario():
    inventory, geometry = inputs()
    draft = plan.draft_plan("mechanism", inventory, geometry, profiles=profiles())
    frozen = plan.freeze_plan(draft)
    assert plan.validate_plan(frozen) is None
    assert len(frozen["runs"]) == 8
    assert {run["scale"] for run in frozen["runs"]} == {"large_database", "large_asset"}
    assert {run["arm"] for run in frozen["runs"]} == {"candidate"}
    assert {run["scenario"] for run in frozen["runs"]} == {"normal", "cancel"}
    assert len({run["execution_id"] for run in frozen["runs"]}) == 8
    assert all(run["attempt"] == 1 for run in frozen["runs"])
    cancel = next(run for run in frozen["runs"] if run["scenario"] == "cancel")
    assert cancel["cancel_trigger"] == "first_budgeted_chunk_after_view_acquired"
    asset_cancel = next(
        run
        for run in frozen["runs"]
        if run["scenario"] == "cancel" and run["scale"] == "large_asset"
    )
    assert asset_cancel["cancel_trigger"] == "first_asset_copy_chunk"
    assert cancel["expected_result"] == "cancelled"
    assert "actual_executor_exit" in frozen["required_evidence"]
    assert frozen["execution_scope"] == "plan_only_not_execution_permission"


def test_matrix_is_gated_has_exact_budget_and_rotates_without_changing_load():
    inventory, geometry = inputs()
    draft = plan.draft_plan("matrix", inventory, geometry, profiles=profiles())
    assert "mechanism_pass_evidence_missing" in draft["gaps"]
    gate = {
        "plan_sha256": "1" * 64,
        "results_sha256": "2" * 64,
        **{key: inventory[key] for key in ("candidate", "fixture_sha256", "tool_sha256")},
    }
    frozen = plan.freeze_plan(
        plan.draft_plan("matrix", inventory, geometry, profiles=profiles(), mechanism_pass=gate)
    )
    for platform in plan.PLATFORMS:
        rows = [run for run in frozen["runs"] if run["platform"] == platform]
        assert len(rows) == 48
        assert sum(run["arm"] == "baseline" for run in rows) == 12
        for scale in plan.SCALES:
            windows = [run["window"] for run in rows if run["scale"] == scale]
            assert all(window == windows[0] for window in windows)
            orders = [
                [run["arm"] for run in rows if run["scale"] == scale and run["group"] == group]
                for group in (1, 2, 3)
            ]
            assert orders == [list(order) for order in plan.ORDERS]
    assert len({run["execution_id"] for run in frozen["runs"]}) == 96


def test_matrix_rejects_mechanism_evidence_for_different_inventory():
    inventory, geometry = inputs()
    gate = {
        "plan_sha256": "1" * 64,
        "results_sha256": "2" * 64,
        **{key: inventory[key] for key in ("candidate", "fixture_sha256", "tool_sha256")},
    }
    gate["candidate"] = "f" * 40
    with pytest.raises(plan.PlanError, match="plan_gate_mismatch"):
        plan.draft_plan("matrix", inventory, geometry, mechanism_pass=gate)


@pytest.mark.parametrize("field", ["candidate", "fixture_sha256", "tool_sha256"])
def test_inventory_is_supplied_not_hardcoded_and_changes_execution_identity(field):
    inventory, geometry = inputs()
    first = plan.draft_plan("mechanism", inventory, geometry)
    inventory[field] = "f" * len(inventory[field])
    if field == "fixture_sha256":
        for item in geometry.values():
            item[field] = inventory[field]
    second = plan.draft_plan("mechanism", inventory, geometry)
    assert first["runs"][0]["execution_id"] != second["runs"][0]["execution_id"]


@pytest.mark.parametrize("mutation", ["seed", "scale", "content", "ledger", "fixture", "platform"])
def test_rejects_changed_approved_geometry(mutation):
    inventory, geometry = inputs()
    item = geometry["Windows"]
    if mutation == "seed":
        item["seed"] = 74
    elif mutation == "scale":
        del item["scales"]["small_text"]
    elif mutation == "content":
        item["scales"]["small_text"]["logical"]["content_bytes_per_record"] = 2048
    elif mutation == "ledger":
        item["scales"]["small_text"]["ledger_rows"]["record"] = 1
    elif mutation == "fixture":
        item["fixture_sha256"] = "0" * 64
    else:
        del geometry["Linux"]
    with pytest.raises(plan.PlanError):
        plan.draft_plan("mechanism", inventory, geometry)


@pytest.mark.parametrize("mutation", ["window", "arm", "attempt", "reference", "gate", "extra"])
def test_validation_recomputes_schedule_not_only_digest(mutation):
    inventory, geometry = inputs()
    frozen = plan.freeze_plan(
        plan.draft_plan("mechanism", inventory, geometry, profiles=profiles())
    )
    if mutation == "window":
        frozen["runs"][0]["window"]["load"]["window_seconds"] += 1
    elif mutation == "arm":
        frozen["runs"][0]["arm"] = "original"
    elif mutation == "attempt":
        frozen["runs"][0]["attempt"] = 2
    elif mutation == "reference":
        frozen["references"]["original"] = "0" * 40
    elif mutation == "gate":
        frozen["parameters"]["view_seconds"] = 600
    else:
        frozen["unexpected"] = True
    frozen["plan_sha256"] = plan.digest({k: v for k, v in frozen.items() if k != "plan_sha256"})
    with pytest.raises(plan.PlanError, match="plan_mismatch"):
        plan.validate_plan(frozen)


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan")])
def test_invalid_estimates_are_rejected(value):
    data = profile()
    data["reserves"]["native"]["seconds"] = value
    with pytest.raises(plan.PlanError):
        plan.derive_window({"memory_database_bytes": 1024}, data)


def test_inputs_are_copied_and_absent_estimate_is_a_gap():
    inventory, geometry = inputs()
    cost = profiles()
    del cost["Windows"]["large_database"]["reserves"]["native"]
    draft = plan.draft_plan("mechanism", inventory, geometry, profiles=cost)
    assert "Windows/large_database:reserve_native_missing" in draft["gaps"]
    saved = copy.deepcopy(draft)
    inventory["candidate"] = "f" * 40
    geometry["Windows"]["seed"] = 99
    cost["Linux"]["large_asset"]["warmup_requests"] = 99
    assert draft == saved
