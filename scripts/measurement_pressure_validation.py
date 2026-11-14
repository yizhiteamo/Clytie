"""Bounded offline predicates over the sibling Index SQLite interface.

No measurement/production calls. `verified` always names a limited predicate,
never a mechanism verdict. Existing wall timestamps never become budget clocks.
"""

CHUNK = 256 * 1024
BURST = 512 * 1024
NORMAL = 16 * 1024 * 1024
BUSY = 4 * 1024 * 1024


class Missing(ValueError):
    pass


class Invalid(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise Invalid(code)


def refill_expected(before, now, owners, signals):
    """Exact existing product expression, only for proposed real-clock snapshots.

    `signals` maps explicit owner hashes to [received,wait,run,final]. The caller
    must establish before/after provenance and the complete owner/signal set.
    No tolerance, inferred time offset, rate fitting or wall-clock substitution.
    """
    require(now >= before["updated"], "budget_clock_regressed")
    fresh = (
        float("-inf")
        if owners is None or any(owner not in signals for owner in owners)
        else min((signals[owner][0] + 1.0 for owner in owners), default=float("inf"))
    )
    start = before["updated"]
    normal_at = (
        start
        if before["normal"]
        else (before["low_since"] + 1.0 if before["low_since"] is not None else float("inf"))
    )
    normal_time = max(0.0, min(now, fresh) - max(start, normal_at))
    elapsed = max(0.0, now - start)
    result = dict(before)
    result["tokens"] = min(
        BURST, before["tokens"] + normal_time * NORMAL + (elapsed - normal_time) * BUSY
    )
    result["updated"] = now
    if now > fresh:
        result["normal"], result["low_since"] = False, None
    elif now >= normal_at:
        result["normal"] = True
    return result


def verify_refill(snapshot):
    expected = refill_expected(
        snapshot["before"], snapshot["after"]["updated"], snapshot["owners"], snapshot["signals"]
    )
    require(expected == snapshot["after"], "token_refill_state_mismatch")


def verify_deduction(refill, charge):
    """Requires the same actual charge context and last refill, not nearest time."""
    for key in ("budget_id", "charge_call_id", "stage_id", "charge_sequence"):
        require(refill[key] == charge[key], "token_charge_identity_mismatch")
    require(charge["outcome"] == "ok", "successful_charge_required")
    size, direction = charge["requested_bytes"], charge["direction"]
    require(type(size) is int and 0 <= size <= CHUNK, "charge_chunk_exceeded")
    require(direction in {"read", "write"}, "charge_direction_invalid")
    before, after = refill["after"], charge["after"]
    require(before["tokens"] >= size, "charge_without_tokens")
    require(after["tokens"] == before["tokens"] - size, "token_deduction_mismatch")
    require(after["updated"] == before["updated"], "unobserved_refill_before_deduction")
    expected = dict(before["charged"])
    expected[direction] += size
    require(after["charged"] == expected, "charge_counter_mismatch")


class Predicates:
    def __init__(self, index):
        self.index, self.db = index, index.db
        self.results = {}
        # Only the new offline analysis DB; no mutation of original raw files.
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS pb_receipts ON rows(phase,json_extract(j,'$.receipt_id'),event);
            CREATE INDEX IF NOT EXISTS pb_budgets ON rows(phase,json_extract(j,'$.pressure_budget_id'),id);
            CREATE INDEX IF NOT EXISTS pb_samples ON rows(phase,json_extract(j,'$.sample_hash'));
        """)

    def rows(self, where, args=(), order="id"):
        return self.index.rows(where, args, order)

    def one(self, where, args=()):
        cursor = self.rows(where, args)
        row = next(cursor, None)
        if row is None:
            raise Missing("required_event_missing")
        require(next(cursor, None) is None, "required_event_duplicated")
        return row

    def exists(self, where, args=()):
        return (
            self.db.execute("SELECT 1 FROM rows WHERE " + where + " LIMIT 1", args).fetchone()
            is not None
        )

    def mark(self, name, status, code):
        result = self.results.setdefault(name, {"status": status, "reasons": []})
        rank = {"verified": 0, "unverified": 1, "failed": 2}
        if rank[status] > rank[result["status"]]:
            result["status"] = status
        if code not in result["reasons"]:
            result["reasons"].append(code)

    def run_check(self, name, function):
        try:
            count = function(name)
            if not count:
                self.mark(name, "unverified", "no_relevant_observations")
        except Missing as error:
            self.mark(name, "unverified", str(error))
        except Invalid as error:
            self.mark(name, "failed", str(error))
        if name not in self.results:
            self.mark(name, "verified", "observed_predicate_only")

    def receipts(self, name):
        count = 0
        for row in self.rows("phase='control_pressure' AND event='received'"):
            count += 1
            receipt = row["receipt_id"]
            end = self.one(
                "phase='control_pressure' AND event='returned' AND json_extract(j,'$.receipt_id')=?",
                (receipt,),
            )
            pressure = self.one(
                "phase='budget_pressure' AND json_extract(j,'$.receipt_id')=?", (receipt,)
            )
            for field in ("pressure_budget_id", "owner_hash", "sample", "sample_hash"):
                require(row[field] == end[field], "control_receipt_changed")
            for field in ("pressure_budget_id", "owner_hash", "receipt_id"):
                require(row[field] == pressure[field], "budget_receipt_identity_mismatch")
            require(row["at_s"] <= pressure["at_s"] <= end["at_s"], "receipt_call_order_invalid")
            sample = row["sample"]
            if row["sample_hash"] is None or any(value is None for value in sample.values()):
                self.mark(name, "unverified", "pressure_sample_incomplete")
                continue
            if pressure["accepted"] is not True or end["outcome"] != "ok":
                self.mark(name, "unverified", "pressure_delivery_rejected_or_unknown")
                continue
            require(
                pressure["waiting_s"] == sample["ready_wait_seconds"]
                and pressure["executing_s"] == sample["execution_seconds"]
                and pressure["finalizer_wait"]
                == (sample["finalizer_capacity_blocked"] or sample["finalizer_wait_seconds"] > 0),
                "pressure_values_not_forwarded",
            )
            if not self.exists(
                "phase='io_control_binding' AND event='session_registered' AND budget=? "
                "AND json_extract(j,'$.session_hash')=? AND json_extract(j,'$.storage_owner')=1 "
                "AND json_extract(j,'$.source')='runtime_object' AND json_extract(j,'$.outcome')='ok' AND t<=?",
                (row["pressure_budget_id"], row["owner_hash"], row["at_s"]),
            ):
                self.mark(name, "unverified", "actual_storage_owner_session_binding_missing")
            if not self.exists(
                "phase='io_slot' AND event='granted' AND budget=?", (row["pressure_budget_id"],)
            ):
                self.mark(name, "unverified", "shared_charge_budget_witness_missing")
        for row in self.rows("phase='budget_pressure'"):
            if row["receipt_id"] is None or not self.exists(
                "phase='control_pressure' AND event='received' AND json_extract(j,'$.receipt_id')=?",
                (row["receipt_id"],),
            ):
                self.mark(name, "unverified", "budget_pressure_control_source_missing")
        return count

    def owner_values(self, name):
        count = 0
        for row in self.rows("phase='control_pressure' AND event='received'"):
            count += 1
            if row["sample_hash"] is None:
                self.mark(name, "unverified", "sample_hash_missing")
                continue
            found = 0
            for witness in self.rows(
                "phase='owner_pressure' AND json_extract(j,'$.sample_hash')=?",
                (row["sample_hash"],),
            ):
                require(witness["sample"] == row["sample"], "owner_sample_value_mismatch")
                found += witness["outcome"] == "ok"
            if not found:
                self.mark(name, "unverified", "actual_owner_sample_value_witness_missing")
        # This verifies observed equal content only, NEVER Owner鈫攕ession identity.
        return count

    def busy(self, name):
        count = 0
        for row in self.rows("phase='budget_pressure' AND json_extract(j,'$.accepted')=1"):
            wait, run, final = row["waiting_s"], row["executing_s"], row["finalizer_wait"]
            if wait is None or run is None or final is None:
                self.mark(name, "unverified", "pressure_scalar_missing")
                continue
            require(wait >= 0 and run >= 0, "negative_pressure_accepted")
            if wait >= 0.020 or run >= 0.020 or final:
                count += 1
                require(
                    row["normal"] is False and row["low_since_monotonic_s"] is None,
                    "busy_pressure_did_not_clear_normal_and_recovery",
                )
        return count

    def stale(self, name):
        count = 0
        for row in self.rows("phase='budget_mode'"):
            owners, signals = row["owners_count"], row["signals_count"]
            if owners is None or signals is None:
                if row["normal"] is True:
                    require(owners is not None, "unknown_owner_set_reported_normal")
                self.mark(name, "unverified", "mode_membership_count_missing")
                continue
            if signals < owners:
                count += 1
                require(
                    row["normal"] is False and row["low_since_monotonic_s"] is None,
                    "missing_owner_signal_reported_normal_or_recovering",
                )
            latest = self.db.execute(
                "SELECT max(json_extract(j,'$.received_monotonic_s')) FROM rows WHERE phase='budget_pressure' "
                "AND json_extract(j,'$.pressure_budget_id')=? AND json_extract(j,'$.accepted')=1 AND id<?",
                (row["pressure_budget_id"], row["id"]),
            ).fetchone()[0]
            clock = row["updated_monotonic_s"]
            if owners > 0 and latest is not None and clock is not None and clock > latest + 1.0:
                count += 1
                require(
                    row["normal"] is False and row["low_since_monotonic_s"] is None,
                    "all_observed_signals_stale_but_normal_or_recovering",
                )
        return count

    def recovery(self, name):
        count = 0
        for (budget,) in self.db.execute(
            "SELECT DISTINCT json_extract(j,'$.pressure_budget_id') FROM rows WHERE phase='budget_mode'"
        ):
            previous = None
            for row in self.rows(
                "phase='budget_mode' AND json_extract(j,'$.pressure_budget_id')=?", (budget,)
            ):
                if previous is not None and previous["normal"] is False and row["normal"] is True:
                    low, clock = row["low_since_monotonic_s"], row["updated_monotonic_s"]
                    if low is None or clock is None:
                        self.mark(name, "unverified", "recovery_clock_boundary_missing")
                    else:
                        count += 1
                        require(clock >= low + 1.0, "normal_recovery_before_recorded_one_second")
                previous = row
        return count

    def charges(self, name):
        count = 0
        for row in self.rows("phase='io_charge' AND event='started'"):
            end = self.one(
                "phase='io_charge' AND event='finished' AND transfer=?", (row["transfer_id"],)
            )
            require(
                all(
                    row[field] == end[field]
                    for field in ("stage_id", "budget_id", "direction", "requested_bytes")
                ),
                "charge_identity_changed",
            )
            require(end["at_s"] >= row["at_s"], "charge_finish_before_start")
            if end["outcome"] == "ok":
                count += 1
                require(0 <= row["requested_bytes"] <= CHUNK, "charge_chunk_exceeded")
                require(
                    end["charged_bytes"] == row["requested_bytes"],
                    "successful_charge_bytes_mismatch",
                )
        return count

    def file_lengths(self, name):
        count = 0
        for row in self.rows("phase='file_batch' AND event='started'"):
            end = self.one(
                "phase='file_batch' AND event='finished' AND transfer=?", (row["transfer_id"],)
            )
            require(
                all(
                    row[field] == end[field]
                    for field in ("stage_id", "direction", "requested_bytes")
                ),
                "file_batch_identity_changed",
            )
            if end["outcome"] == "ok" and row["requested_bytes"] is not None:
                count += 1
                require(
                    0 <= end["actual_bytes"] <= row["requested_bytes"],
                    "file_actual_exceeds_request",
                )
        return count

    def sqlite_geometry(self, name):
        count = 0
        for start in self.rows("phase='sqlite_batch' AND event='started'"):
            transfer = start["transfer_id"]
            geometry = self.one(
                "phase='sqlite_geometry' AND event='backup_started' AND transfer=?", (transfer,)
            )
            size, pages = geometry["page_size"], geometry["page_count"]
            if size is None or pages is None:
                self.mark(name, "unverified", "actual_sqlite_geometry_missing")
                continue
            require(
                size > 0 and pages >= 0 and start["pages_per_step"] > 0, "sqlite_geometry_invalid"
            )
            require(size * start["pages_per_step"] <= CHUNK, "sqlite_step_exceeds_frozen_chunk")
            require(start["stage_id"] == geometry["stage_id"], "sqlite_geometry_stage_mismatch")
            previous, progress_count, terminal = pages, 0, False
            for row in self.rows(
                "phase='sqlite_batch' AND event='progress' AND transfer=?", (transfer,)
            ):
                if row["status"] not in {0, 101}:
                    self.mark(name, "unverified", "sqlite_non_success_progress_observed")
                    continue
                progress_count += 1
                delta = previous - row["remaining_pages"]
                require(
                    row["total_pages"] == pages and 0 < delta <= start["pages_per_step"],
                    "sqlite_fixed_view_progress_mismatch",
                )
                require(row["copied_pages"] == delta, "sqlite_observed_page_delta_mismatch")
                require(not terminal, "sqlite_progress_after_done")
                previous = row["remaining_pages"]
                terminal = row["status"] == 101
                if terminal:
                    require(previous == 0, "sqlite_done_with_remaining_pages")
            end = self.one("phase='sqlite_batch' AND event='finished' AND transfer=?", (transfer,))
            if end["outcome"] == "ok":
                require(
                    progress_count > 0 and terminal and previous == 0,
                    "sqlite_success_without_full_copy",
                )
                count += 1
        return count

    def source_bindings(self, name):
        count = 0
        for row in self.rows("phase='control_pressure' AND event='received'"):
            binding = self.one(
                "phase='pressure_source_binding' AND budget=? AND json_extract(j,'$.owner_hash')=? AND t<=?",
                (row["pressure_budget_id"], row["owner_hash"], row["at_s"]),
            )
            if not self.exists(
                "phase='owner_pressure' AND json_extract(j,'$.owner_id')=? "
                "AND json_extract(j,'$.sample_hash')=? AND t<=?",
                (binding["owner_id"], row["sample_hash"], row["at_s"]),
            ):
                raise Missing("bound_owner_sample_missing")
            count += 1
        return count

    def accounting(self, name):
        count, deductions = 0, 0
        for (budget,) in self.db.execute(
            "SELECT DISTINCT budget FROM rows WHERE phase='budget_accounting'"
        ):
            previous = None
            for row in self.rows("phase='budget_accounting' AND budget=?", (budget,)):
                if row["outcome"] != "ok" or row["after"] is None:
                    self.mark(name, "unverified", "accounting_call_not_completed")
                    continue
                after = row["after"]
                if row["event"] == "budget_created":
                    require(previous is None, "budget_initialization_repeated")
                    require(
                        after["tokens"] == BURST and after["charged"] == {"read": 0, "write": 0},
                        "initial_burst_or_counter_changed",
                    )
                    previous = after
                    continue
                if previous is None:
                    self.mark(name, "unverified", "budget_initialization_missing")
                if row["event"] == "refill_returned":
                    if row["before"] is None or row["signals"] is None:
                        raise Missing("refill_state_missing")
                    if previous is not None:
                        require(
                            all(
                                row["before"][field] == previous[field]
                                for field in ("tokens", "updated", "charged")
                            ),
                            "unobserved_budget_balance_change",
                        )
                    verify_refill(row)
                    count += 1
                elif row["event"] == "charge_returned":
                    if row["refill_id"] is None:
                        raise Missing("charge_refill_context_missing")
                    refill = self.one(
                        "phase='budget_accounting' AND event='refill_returned' "
                        "AND json_extract(j,'$.refill_id')=?",
                        (row["refill_id"],),
                    )
                    require(refill["outcome"] == "ok", "charge_after_failed_refill")
                    verify_deduction(refill, row)
                    observed = self.one(
                        "phase='io_charge' AND event='finished' AND budget=? AND stage=? "
                        "AND json_extract(j,'$.charge_sequence')=?",
                        (budget, row["stage_id"], row["charge_sequence"]),
                    )
                    require(
                        observed["outcome"] == "ok"
                        and observed["charged_bytes"] == row["requested_bytes"]
                        and observed["direction"] == row["direction"],
                        "accounting_charge_stream_mismatch",
                    )
                    count += 1
                    deductions += 1
                previous = after
        if not deductions:
            self.mark(name, "unverified", "no_observed_deduction")
        return count

    def report(self):
        for name, function in (
            ("pressure_receipt_budget_binding", self.receipts),
            ("owner_sample_value_witness", self.owner_values),
            ("pressure_busy_threshold_observed", self.busy),
            ("pressure_observed_stale_or_missing_signal", self.stale),
            ("pressure_recovery_recorded_minimum", self.recovery),
            ("charge_success_bytes_and_chunk", self.charges),
            ("file_request_actual_lengths", self.file_lengths),
            ("sqlite_fixed_view_batch_geometry", self.sqlite_geometry),
            ("pressure_owner_identity", self.source_bindings),
            ("token_16_4_mibps_512kib_model", self.accounting),
        ):
            self.run_check(name, function)
        for name, code in (
            (
                "pressure_complete_stale_and_low_recovery",
                "exact_owner_membership_and_complete_clock_state_absent",
            ),
            (
                "file_charge_causal_binding",
                "checked_separately_in_batch_reservations_host_file_batch_reservations",
            ),
            (
                "sqlite_charge_causal_binding",
                "checked_separately_in_batch_reservations_sqlite_logical_progress_reservations",
            ),
            (
                "all_heavy_io_coverage",
                "native_internal_bytes_unknown_plugin_bypass_unobserved_and_scope_incomplete",
            ),
        ):
            self.mark(name, "unverified", code)
        return dict(
            mechanism_acceptance="not_evaluated",
            requirements=self.results,
            frozen_parameters=dict(
                normal_bytes_s=NORMAL, busy_bytes_s=BUSY, burst_bytes=BURST, chunk_bytes=CHUNK
            ),
            clock_policy="production_monotonic_only_no_wall_conversion",
            native_internal_read_bytes=None,
            plugin_internal_io="unobserved",
        )
