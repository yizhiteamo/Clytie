"""Offline observed predicates only. Never a mechanism acceptance gate.

Uses existing lifecycle validators; owns no measurement or product execution.
Source result and v2 unverified coverage remain unchanged.
"""

if __package__:
    from . import measurement_evidence as evidence
    from .measurement_analysis_index import (
        DiskMap,
        DiskSet,
        Index,
        Invalid,
        Missing,
        RequiredKeys,
        require,
    )
    from .measurement_batch_validation import BatchPredicates
    from .measurement_pressure_validation import Predicates
    from .measurement_sample_validation import ProcessExits, RequestPlan, ResourceReleases
    from .measurement_wal_validation import WalPredicates
else:
    import measurement_evidence as evidence
    from measurement_analysis_index import (
        DiskMap,
        DiskSet,
        Index,
        Invalid,
        Missing,
        RequiredKeys,
        require,
    )
    from measurement_batch_validation import BatchPredicates
    from measurement_pressure_validation import Predicates
    from measurement_sample_validation import ProcessExits, RequestPlan, ResourceReleases
    from measurement_wal_validation import WalPredicates


class MechanismValidation(Index):
    def __init__(self, source, target, expected):
        super().__init__(source, target, expected)
        try:
            self.db.executescript("""
            CREATE TABLE slots(stage TEXT PRIMARY KEY,budget INTEGER,seq INTEGER,
                start REAL,end REAL,grant_id INTEGER,release_id INTEGER);
            CREATE TABLE findings(check_name TEXT,status TEXT,code TEXT,
                UNIQUE(check_name,status,code));
            """)
        except BaseException:
            self.db.close()
            raise

    def mark(self, name, status, code):
        self.db.execute("INSERT OR IGNORE INTO findings VALUES(?,?,?)", (name, status, code))

    def checked(self, name, function):
        try:
            function()
        except Missing as error:
            self.mark(name, "unverified", str(error))
        except (Invalid, evidence.EvidenceError, ValueError) as error:
            status = "unverified" if str(error).endswith("_missing") else "failed"
            self.mark(name, status, str(error))
        if not self.db.execute("SELECT 1 FROM findings WHERE check_name=?", (name,)).fetchone():
            self.mark(name, "verified", "observed_predicate_only")

    def requests(self):
        expected = "cancelled" if self.run.get("scenario") == "cancel" else "ok"
        plan = RequestPlan(self.run, expected_backup=expected)
        for row in self.rows("file='requests.jsonl'"):
            plan.add(row)
        plan.complete()
        require(
            plan.backups[0]["finished_s"] <= plan.start + self.run["load"]["window_seconds"],
            "backup_window_uncovered",
        )
        self.window_end = plan.start + self.run["load"]["window_seconds"]
        self.backup = plan.backups[0]
        self.business_finish = plan.last_finish

    def execution_window(self):
        self.requests()
        processes = ProcessExits(self.run)
        for row in self.rows("file='release-events.jsonl'"):
            processes.add(row)
        processes.complete()
        latest = self.db.execute(
            "SELECT max(t) FROM rows WHERE phase='native_validation'"
        ).fetchone()[0]
        if latest is None:
            raise Missing("native_window_observation_missing")
        end = max(latest, max(processes.exited.values(), default=0), self.backup["finished_s"])
        require(
            end <= min(self.window_end, self.business_finish), "native_business_window_uncovered"
        )

    def releases(self):
        name = "resource_lifecycles"
        validator = ResourceReleases(self.run)
        validator.states = DiskMap(self.db, "release_states")
        validator.required = RequiredKeys(validator.required)
        processes = ProcessExits(self.run)
        observed = 0
        for row in self.rows("file='release-events.jsonl'"):
            observed += 1
            require(
                row["id"] >= 1 and (row["resource"] != "view" or row["id"] >= 2),
                "measurement_resource_identity_invalid",
            )
            if row["resource"] == "process":
                require(row["id"] in processes.expected, "measurement_process_not_planned")
            require(
                row["event"]
                in {
                    "acquired",
                    "cancel_requested",
                    "io_returned",
                    "unknown",
                    "failed",
                    validator.TERMINALS[row["resource"]],
                },
                "measurement_resource_terminal_invalid",
            )
            if row["event"] != "acquired" and not self.count(
                "file='release-events.jsonl' AND id=? AND json_extract(j,'$.resource')=? AND event='acquired'",
                (row["id"], row["resource"]),
            ):
                if row["event"] in {"failed", "unknown"}:
                    raise Invalid("measurement_resource_release_unconfirmed")
                self.mark(name, "unverified", "resource_acquisition_missing")
                continue
            processes.add(row)
            validator.add(row)
            validator.states.flush()
        if not observed:
            raise Missing("resource_events_missing")
        processes.complete()
        validator.complete()
        self.process_exits = processes.exited
        self.mark(name, "verified", "existing_release_and_process_semantics")

    def io_lifecycles(self):
        # Enable existing completion predicates for cancelled samples without
        # rewriting their stored result or claiming successful product outcomes.
        validator = evidence.io_tools().IoEvents({**self.run, "result": "pass"})
        validator.seen = DiskSet(self.db, "io_seen")
        validator.transfers = DiskSet(self.db, "io_transfers")
        validator.active = DiskMap(self.db, "io_active")
        validator.slots = DiskMap(self.db, "io_slots")
        validator.capabilities = DiskMap(self.db, "io_capabilities")
        observed = 0
        for row in self.rows("file='storage-phases.jsonl'"):
            observed += row["phase"] in evidence.IO_PHASES
            if (
                row["phase"] in {"file_batch", "sqlite_batch", "io_charge"}
                and row["event"] != "started"
            ):
                if not self.count(
                    "phase=? AND transfer=? AND event='started'", (row["phase"], row["transfer_id"])
                ):
                    self.mark("io_lifecycles", "unverified", "io_transfer_start_missing")
                    continue
            if row["phase"] == "io_slot" and row["event"] == "released":
                if not self.count(
                    "phase='io_slot' AND stage=? AND budget=? AND event='granted'",
                    (row["stage_id"], row["budget_id"]),
                ):
                    self.mark("io_lifecycles", "unverified", "io_grant_missing")
                    continue
            validator.add(row)
            validator.active.flush()
            validator.slots.flush()
            validator.capabilities.flush()
        if not observed:
            raise Missing("io_events_missing")
        try:
            validator.complete()
        except evidence.EvidenceError as error:
            if str(error) == "io_lifecycle_incomplete":
                raise Missing("io_lifecycle_incomplete") from None
            raise

    def native_lifecycles(self):
        observed, failed, latest = 0, False, 0
        for (identifier,) in self.db.execute(
            "SELECT DISTINCT validation FROM rows WHERE phase='native_validation'"
        ):
            observed += 1
            if not self.count(
                "phase='native_validation' AND validation=? AND event='started'", (identifier,)
            ):
                self.mark("native_lifecycles", "unverified", "native_start_missing")
                continue
            validator = evidence.NativeEvents()
            for row in self.rows("phase='native_validation' AND validation=?", (identifier,)):
                try:
                    validator.add(row)
                except evidence.EvidenceError as error:
                    if str(error) == "native_release_unconfirmed" and self.native_missing_proof(
                        identifier
                    ):
                        raise Missing("native_release_unconfirmed") from None
                    raise
                if row["event"] == "started":
                    state = validator.active[identifier]
                    for key in ("attached", "closed", "unknown"):
                        state[key] = DiskSet(self.db, "native_" + str(identifier) + "_" + key)
            # Missing process proof cannot be substituted by another child's exit.
            try:
                validator.complete(process_exits=getattr(self, "process_exits", {}))
            except evidence.EvidenceError as error:
                if str(error) == "native_release_unconfirmed" and self.native_missing_proof(
                    identifier
                ):
                    raise Missing("native_release_unconfirmed") from None
                raise
            failed = failed or validator.failed
            latest = max(latest, validator.latest)
        if not observed:
            raise Missing("native_events_missing")
        require(not (self.run["result"] == "pass" and failed), "native_failure_with_success")
        self.native_latest = latest

    def native_missing_proof(self, identifier):
        for event in ("started", "driver_finished", "finished"):
            if not self.count(
                "phase='native_validation' AND validation=? AND event=?", (identifier, event)
            ):
                return True
        for row in self.rows(
            "phase='native_validation' AND validation=? AND event='connection_attached'",
            (identifier,),
        ):
            if not self.count(
                "phase='native_validation' AND validation=? AND connection=? AND event='connection_closed'",
                (identifier, row["connection_id"]),
            ):
                return True
        return False

    def wal_associations(self):
        if __package__:
            from .measurement_pressure_observation import WalBindings
        else:
            from measurement_pressure_observation import WalBindings

        observed = 0
        for (wal,) in self.db.execute(
            "SELECT DISTINCT json_extract(j,'$.wal_id') FROM rows WHERE phase IN ('wal_growth','wal_binding','sqlite_geometry') AND json_extract(j,'$.wal_id') IS NOT NULL"
        ):
            observed += 1
            validator = WalBindings()
            # Each one-WAL group is bounded (sets hold one ID); cross-group
            # snapshot/limit/database invariants are checked below on disk.
            for row in self.rows(
                "phase IN ('wal_growth','wal_binding','sqlite_geometry') AND json_extract(j,'$.wal_id')=?",
                (wal,),
            ):
                validator.add("storage-phases.jsonl", row)
            for binding in validator.bindings.values():
                for row in self.rows(
                    "file='release-events.jsonl' AND json_extract(j,'$.resource')='view' AND id=?",
                    (binding["view_id"],),
                ):
                    validator.add("release-events.jsonl", row)
            validator.complete()
        if not observed:
            raise Missing("wal_events_missing")
        for key, value in (("snapshot_id", "view_id"), ("limits_id", "snapshot_id")):
            require(
                not self.db.execute(
                    f"SELECT 1 FROM rows WHERE phase='wal_binding' GROUP BY json_extract(j,'$.{key}') HAVING count(DISTINCT json_extract(j,'$.{value}'))>1 LIMIT 1"
                ).fetchone(),
                "wal_binding_inconsistent",
            )
        require(
            not self.db.execute(
                "SELECT 1 FROM rows WHERE phase='wal_binding' GROUP BY json_extract(j,'$.limits_id'),json_extract(j,'$.database_hash') HAVING count(*)>1 LIMIT 1"
            ).fetchone(),
            "wal_binding_inconsistent",
        )

    def planned_cancel(self):
        if self.run["scenario"] != "cancel":
            require(
                not self.count("phase='planned_cancel'"), "unexpected_cancel_in_normal_scenario"
            )
            self.mark("planned_cancel_binding", "unverified", "not_cancel_scenario")
            return
        require(
            self.run["result"] == "cancelled"
            and self.run["scale"] in {"large_database", "large_asset"},
            "planned_cancel_manifest_mismatch",
        )
        require(
            not self.count("phase='planned_cancel' AND event IN ('rejected','failed')"),
            "cancel_explicit_failure",
        )
        count = self.count("phase='planned_cancel'")
        if count < 4:
            raise Missing("cancel_lifecycle_missing")
        require(count == 4, "cancel_lifecycle_extra")
        trigger, requested, accepted, terminal = (
            self.one("phase='planned_cancel' AND event=?", (event,))
            for event in ("triggered", "requested", "accepted", "terminal")
        )
        observed_order = iter(self.rows("phase='planned_cancel'"))
        for event in ("triggered", "requested", "accepted", "terminal"):
            require(next(observed_order)["event"] == event, "cancel_lifecycle_order")
        binding = ("trigger", "source_row_id", "source_at_s", "stage_id", "view_id")
        require(
            all(
                all(row[key] == trigger[key] for key in binding)
                for row in (requested, accepted, terminal)
            ),
            "cancel_binding_changed",
        )
        source = self.one("file='storage-phases.jsonl' AND id=?", (trigger["source_row_id"],))
        require(
            source.get("stage_id") == trigger["stage_id"]
            and source.get("at_s") == trigger["source_at_s"]
            and source.get("outcome") == "ok"
            and source.get("process_sequence") == 0,
            "cancel_source_mismatch",
        )
        database = self.run["scale"] == "large_database"
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
        require(matches, "cancel_trigger_invalid")
        # Recheck full request proof here if the independent request predicate
        # failed; no unchecked dependency may turn a cancel claim verified.
        self.requests()
        require(
            self.backup["started_s"]
            <= source["at_s"]
            <= trigger["at_s"]
            <= requested["at_s"]
            <= accepted["at_s"]
            <= terminal["at_s"]
            <= self.backup["finished_s"]
            and terminal["terminal_phase"] == "cancelled"
            and terminal["cancel_requested"] is True,
            "cancel_terminal_or_order_invalid",
        )
        if database:
            view = self.one(
                "file='release-events.jsonl' AND json_extract(j,'$.resource')='view' AND id=? AND event='acquired'",
                (trigger["view_id"],),
            )
            require(view["at_s"] <= source["at_s"], "cancel_view_after_trigger")
        # Resource conclusion is deliberately separate. This proves the four
        # cancel records and their source/window, never says cancel equals exit.

    def slots(self):
        count = 0
        for row in self.rows("phase='io_slot'", order="t,id"):
            count += 1
            if row["event"] == "granted":
                require(
                    not self.db.execute(
                        "SELECT 1 FROM slots WHERE budget=? AND end IS NULL", (row["budget_id"],)
                    ).fetchone(),
                    "same_budget_concurrent_grant",
                )
                require(
                    not self.db.execute(
                        "SELECT 1 FROM slots WHERE stage=?", (row["stage_id"],)
                    ).fetchone(),
                    "stage_reuse_ambiguous",
                )
                self.db.execute(
                    "INSERT INTO slots VALUES(?,?,?,?,NULL,?,NULL)",
                    (
                        row["stage_id"],
                        row["budget_id"],
                        row["process_sequence"],
                        row["at_s"],
                        row["id"],
                    ),
                )
            else:
                match = self.db.execute(
                    "SELECT budget,seq,start,end FROM slots WHERE stage=?", (row["stage_id"],)
                ).fetchone()
                require(
                    match is not None
                    and match[:2] == (row["budget_id"], row["process_sequence"])
                    and match[3] is None
                    and match[2] <= row["at_s"],
                    "slot_release_unbound",
                )
                self.db.execute(
                    "UPDATE slots SET end=?,release_id=? WHERE stage=?",
                    (row["at_s"], row["id"], row["stage_id"]),
                )
        if not count:
            self.mark("slot_intervals", "unverified", "no_slot_events")
            return
        require(
            not self.db.execute("SELECT 1 FROM slots WHERE end IS NULL").fetchone(),
            "slot_release_missing",
        )
        require(
            not self.db.execute(
                "SELECT 1 FROM slots a JOIN slots b ON a.stage<b.stage AND a.start<b.end AND b.start<a.end LIMIT 1"
            ).fetchone(),
            "observed_global_slot_overlap",
        )

    def control(self):
        name = "slot_control_identity"
        if not self.count("phase='io_control_binding'"):
            self.mark(name, "unverified", "control_observation_missing")
            return
        # Each call is checked independently with a disk-backed lookup.
        stable = (
            "budget_id",
            "instance_hash",
            "controller_hash",
            "session_hash",
            "operation_hash",
            "stage_id",
            "generation_hash",
            "execution_hash",
            "responsibility_controller_hash",
            "call_hash",
            "action",
            "role",
            "responsibility_state",
            "storage_owner",
            "process_sequence",
        )
        for row in self.rows("phase='io_control_binding' AND event='request_started'"):
            call = row["call_hash"]
            require(
                self.count(
                    "phase='io_control_binding' AND event='request_started' AND json_extract(j,'$.call_hash')=?",
                    (call,),
                )
                == 1,
                "control_call_duplicate",
            )
            end = self.one(
                "phase='io_control_binding' AND event='request_finished' AND json_extract(j,'$.call_hash')=?",
                (call,),
            )
            require(
                end["at_s"] >= row["at_s"] and all(end[k] == row[k] for k in stable),
                "control_call_changed",
            )
        for end in self.rows("phase='io_control_binding' AND event='request_finished'"):
            require(
                self.count(
                    "phase='io_control_binding' AND event='request_started' AND json_extract(j,'$.call_hash')=?",
                    (end["call_hash"],),
                )
                == 1,
                "control_call_start_missing",
            )
        if self.count("phase='io_control_binding' AND event='ledger_failed'"):
            self.mark(name, "unverified", "ledger_failure_observed")
        identity = None
        slots = 0
        immutable = (
            "instance_hash",
            "controller_hash",
            "budget_id",
            "session_hash",
            "operation_hash",
            "generation_hash",
            "execution_hash",
            "responsibility_controller_hash",
            "role",
        )
        for stage, budget, sequence, start, finish in self.db.execute(
            "SELECT stage,budget,seq,start,end FROM slots"
        ):
            slots += 1
            loads = self.rows(
                "phase='io_control_binding' AND event='ledger_loaded' AND budget=? AND t<=?",
                (budget, start),
            )
            binding = None
            for load in loads:
                if load["source"] != "validated_ledger" or load["outcome"] != "ok":
                    continue
                current = load["instance_hash"], load["controller_hash"]
                require(None not in current, "loaded_identity_unknown")
                require(binding is None or current == binding, "controller_identity_changed")
                binding = current
            if binding is None:
                self.mark(name, "unverified", "slot_validated_instance_missing")
                continue
            if not self.count(
                "phase='io_control_binding' AND event='control_created' AND budget=? AND json_extract(j,'$.controller_hash')=? AND json_extract(j,'$.source')='runtime_object' AND json_extract(j,'$.outcome')='ok' AND t<=?",
                (budget, binding[1], start),
            ):
                self.mark(name, "unverified", "runtime_control_creation_missing")
            if identity is None:
                identity = binding
            elif identity != binding:
                self.mark(name, "unverified", "multiple_controller_or_instance_scope")
            candidates = self.rows(
                "phase='io_control_binding' AND event='request_started' AND stage=? AND budget=? AND json_extract(j,'$.action')='acquire' AND t<=?",
                (stage, budget, start),
            )
            request = next(candidates, None)
            if request is None:
                self.mark(name, "unverified", "slot_authorized_request_missing")
                continue
            require(next(candidates, None) is None, "slot_acquire_ambiguous")
            require(request["source"] == "runtime_object", "slot_request_source_invalid")
            require(
                (request["instance_hash"], request["controller_hash"]) == binding,
                "slot_control_identity_mismatch",
            )
            if (
                any(request[k] is None for k in immutable)
                or request["responsibility_state"] != "active"
            ):
                self.mark(name, "unverified", "responsibility_identity_missing")
                continue
            require(
                request["responsibility_controller_hash"] == request["controller_hash"],
                "responsibility_controller_mismatch",
            )
            require(request["process_sequence"] == sequence, "slot_request_process_mismatch")
            if not self.count(
                "phase='io_control_binding' AND event='session_registered' AND budget=? AND json_extract(j,'$.controller_hash')=? AND json_extract(j,'$.session_hash')=? AND json_extract(j,'$.source')='runtime_object' AND json_extract(j,'$.outcome')='ok' AND t<=?",
                (budget, binding[1], request["session_hash"], request["at_s"]),
            ):
                self.mark(name, "unverified", "runtime_session_missing")
            end = self.one(
                "phase='io_control_binding' AND event='request_finished' AND json_extract(j,'$.call_hash')=?",
                (request["call_hash"],),
            )
            require(end["outcome"] == "ok" and end["at_s"] >= start, "slot_acquire_failed")
            for event in ("endpoint_bound", "responsibility_observed"):
                matched = False
                for other in self.rows(
                    "phase='io_control_binding' AND event=? AND budget=? AND t<=?",
                    (event, budget, request["at_s"]),
                ):
                    if (
                        all(other[k] == request[k] for k in immutable)
                        and other["outcome"] == "ok"
                        and other["responsibility_state"] == "active"
                        and other["source"]
                        in (
                            {"runtime_object"}
                            if event == "endpoint_bound"
                            else {"validated_ledger", "durable_save"}
                        )
                    ):
                        matched = True
                if not matched:
                    self.mark(name, "unverified", event + "_missing")
            # A stale active row cannot authorize execution after an ended row.
            latest = next(
                self.rows(
                    "phase='io_control_binding' AND event='responsibility_observed' AND json_extract(j,'$.generation_hash')=? AND t<=?",
                    (request["generation_hash"], request["at_s"]),
                    order="t DESC,id DESC",
                ),
                None,
            )
            if latest is None:
                self.mark(name, "unverified", "responsibility_history_missing")
            else:
                require(
                    latest["responsibility_state"] == "active",
                    "responsibility_not_active_at_acquire",
                )
        if not slots:
            self.mark(name, "unverified", "no_slot_events")

    def authorized_release_durable_end(self):
        """Only observed stage authorization and durable metadata, not all resources."""
        name = "authorized_release_durable_end"
        immutable = (
            "process_sequence",
            "budget_id",
            "instance_hash",
            "controller_hash",
            "session_hash",
            "operation_hash",
            "generation_hash",
            "execution_hash",
            "responsibility_controller_hash",
            "role",
        )
        seen = False
        for stage, budget, sequence, granted, released in self.db.execute(
            "SELECT stage,budget,seq,start,end FROM slots"
        ):
            seen = True
            acquire = self.one(
                "phase='io_control_binding' AND event='request_started' AND "
                "stage=? AND budget=? AND json_extract(j,'$.action')='acquire'",
                (stage, budget),
            )
            if any(acquire.get(k) is None for k in immutable):
                self.mark(name, "unverified", "release_authority_identity_missing")
                continue
            if released is None:
                self.mark(name, "unverified", "slot_release_missing")
                continue
            starts = self.rows(
                "phase='io_control_binding' AND event='request_started' AND "
                "stage=? AND budget=? AND json_extract(j,'$.action')='release'",
                (stage, budget),
            )
            start = next(starts, None)
            if start is None:
                # executor_ended can free the slot without a protocol release.
                # Only an explicit wait/identity binding can prove that branch.
                self.mark(name, "unverified", "trusted_release_or_bound_exit_missing")
                continue
            require(next(starts, None) is None, "release_request_ambiguous")
            end = self.one(
                "phase='io_control_binding' AND event='request_finished' AND "
                "json_extract(j,'$.call_hash')=?",
                (start["call_hash"],),
            )
            require(
                all(start[k] == acquire[k] == end[k] for k in immutable)
                and start["stage_id"] == end["stage_id"] == stage
                and start["action"] == end["action"] == "release"
                and start["source"] == end["source"] == "runtime_object"
                and start["responsibility_state"] == end["responsibility_state"] == "active",
                "release_authority_changed",
            )
            require(end["outcome"] == "ok", "release_request_failed")
            require(granted <= start["at_s"] <= released <= end["at_s"], "release_order_invalid")
            # Same generation must retain its complete durable identity. Never
            # connect an ordinal or PID to an execution hash by proximity.
            durable = None
            for row in self.rows(
                "phase='io_control_binding' AND event='responsibility_observed' AND "
                "json_extract(j,'$.generation_hash')=?",
                (acquire["generation_hash"],),
                order="t,id",
            ):
                if row["responsibility_state"] != "ended":
                    continue
                require(
                    all(row[k] == acquire[k] for k in immutable), "release_durable_identity_changed"
                )
                require(row["at_s"] >= end["at_s"], "responsibility_ended_before_release")
                if row["source"] == "durable_save" and row["outcome"] == "ok":
                    durable = row
            if durable is None:
                self.mark(name, "unverified", "durable_ended_missing")
            if self.count(
                "phase='io_control_binding' AND event='ledger_failed' AND budget=?",
                (budget,),
            ):
                self.mark(name, "failed", "ledger_failure_observed")
        if not seen:
            self.mark(name, "unverified", "release_grant_missing")

    def observed_snapshot_release_order(self):
        name = "observed_snapshot_release_order"
        identity = (
            "instance_hash",
            "controller_hash",
            "budget_id",
            "session_hash",
            "operation_hash",
            "generation_hash",
            "execution_hash",
            "responsibility_controller_hash",
            "role",
        )
        total = 0
        for acquired in self.rows("phase='io_control_binding' AND event='snapshot_acquired'"):
            total += 1
            key = acquired.get("resource_id")
            if key is None or acquired.get("operation_hash") is None:
                self.mark(name, "unverified", "snapshot_operation_binding_missing")
                continue
            associated = "phase='io_control_binding' AND json_extract(j,'$.resource_id')=?"
            require(
                self.count(associated + " AND event='snapshot_acquired'", (key,)) == 1,
                "snapshot_acquisition_duplicate",
            )
            stage = self.one(associated + " AND event='snapshot_stage'", (key,))
            closed = self.one(associated + " AND event='snapshot_closed'", (key,))
            joined = self.one(associated + " AND event='snapshot_thread_exited'", (key,))
            for row in (stage, closed, joined):
                require(
                    row["operation_hash"] == acquired["operation_hash"]
                    and row["process_sequence"] == acquired["process_sequence"]
                    and row["source"] == "runtime_object"
                    and row["outcome"] == "ok",
                    "snapshot_binding_changed",
                )
            if stage.get("stage_id") is None or stage.get("view_id") is None:
                self.mark(name, "unverified", "snapshot_stage_or_view_binding_missing")
                continue
            require(
                all(
                    row["stage_id"] == stage["stage_id"] and row["view_id"] == stage["view_id"]
                    for row in (closed, joined)
                ),
                "snapshot_stage_or_view_changed",
            )
            request = self.one(
                "phase='io_control_binding' AND event='request_started' AND "
                "stage=? AND json_extract(j,'$.action')='acquire'",
                (stage["stage_id"],),
            )
            require(
                request["operation_hash"] == acquired["operation_hash"],
                "snapshot_authorized_operation_mismatch",
            )
            if any(request[k] is None for k in identity):
                self.mark(name, "unverified", "snapshot_authority_missing")
                continue
            slot = self.db.execute(
                "SELECT start,end FROM slots WHERE stage=?", (stage["stage_id"],)
            ).fetchone()
            if slot is None or slot[1] is None:
                self.mark(name, "unverified", "snapshot_grant_missing")
                continue
            # Read view closes within the stage. The idle snapshot thread can
            # exit after stage release, but must exit before durable retirement.
            require(
                acquired["at_s"] <= stage["at_s"] <= closed["at_s"] <= joined["at_s"]
                and slot[0] <= stage["at_s"] <= closed["at_s"] <= slot[1],
                "snapshot_release_order_invalid",
            )
            revoked = self.one(
                "phase='io_control_binding' AND event='authorization_revoked' "
                "AND json_extract(j,'$.generation_hash')=?",
                (request["generation_hash"],),
            )
            ended = self.one(
                "phase='io_control_binding' AND event='responsibility_observed' "
                "AND json_extract(j,'$.generation_hash')=? "
                "AND json_extract(j,'$.responsibility_state')='ended' "
                "AND json_extract(j,'$.source')='durable_save'",
                (request["generation_hash"],),
            )
            require(
                all(row[k] == request[k] for row in (revoked, ended) for k in identity),
                "snapshot_retirement_identity_changed",
            )
            require(
                joined["at_s"] <= revoked["at_s"] <= ended["at_s"]
                and revoked["outcome"] == ended["outcome"] == "ok",
                "snapshot_retired_before_resource_exit",
            )
        if not total:
            self.mark(name, "unverified", "snapshot_binding_events_missing")

    def native(self):
        name, count = "native_grant_containment", 0
        for (identifier,) in self.db.execute(
            "SELECT DISTINCT validation FROM rows WHERE phase='native_validation'"
        ):
            count += 1
            start = self.one(
                "phase='native_validation' AND validation=? AND event='started'", (identifier,)
            )
            end = self.one(
                "phase='native_validation' AND validation=? AND event='driver_finished'",
                (identifier,),
            )
            outer = self.one(
                "phase='native_validation' AND validation=? AND event='finished'", (identifier,)
            )
            require(start["at_s"] <= end["at_s"] <= outer["at_s"], "native_driver_order")
            context = (
                "stage_id",
                "io_scope_state",
                "stage_thread_matches",
                "cleanup_source",
                "boundary",
                "request_id",
            )
            for row in self.rows("phase='native_validation' AND validation=?", (identifier,)):
                require(all(row.get(k) == start.get(k) for k in context), "native_context_changed")
                require(row["internal_read_bytes"] is None, "native_bytes_must_remain_unknown")
                require(start["at_s"] <= row["at_s"] <= outer["at_s"], "native_event_order")
            for event in ("cancel_accepted", "interrupt_accepted"):
                require(
                    self.count(
                        "phase='native_validation' AND validation=? AND event=?",
                        (identifier, event),
                    )
                    <= 1,
                    "native_cancel_duplicate",
                )
                for row in self.rows(
                    "phase='native_validation' AND validation=? AND event=?", (identifier, event)
                ):
                    require(row["at_s"] <= end["at_s"], "native_cancel_after_driver")
            require(
                not self.count(
                    "phase='native_validation' AND validation=? AND event='connection_close_unknown'",
                    (identifier,),
                ),
                "native_close_unknown",
            )
            closes = end["at_s"]
            if not self.count(
                "phase='native_validation' AND validation=? AND event='connection_attached'",
                (identifier,),
            ):
                self.mark(name, "unverified", "native_connection_observation_missing")
            for attached in self.rows(
                "phase='native_validation' AND validation=? AND event='connection_attached'",
                (identifier,),
            ):
                connection = attached["connection_id"]
                require(
                    self.count(
                        "phase='native_validation' AND validation=? AND connection=? AND event='connection_attached'",
                        (identifier, connection),
                    )
                    == 1,
                    "native_connection_duplicate",
                )
                closed = self.one(
                    "phase='native_validation' AND validation=? AND connection=? AND event='connection_closed'",
                    (identifier, connection),
                )
                require(
                    end["at_s"] <= closed["at_s"] <= outer["at_s"]
                    and attached["at_s"] <= end["at_s"],
                    "native_close_order",
                )
                closes = max(closes, closed["at_s"])
            for closed in self.rows(
                "phase='native_validation' AND validation=? AND event='connection_closed'",
                (identifier,),
            ):
                require(
                    self.count(
                        "phase='native_validation' AND validation=? AND connection=? AND event='connection_attached'",
                        (identifier, closed["connection_id"]),
                    )
                    == 1,
                    "native_close_unattached",
                )
            if start.get("stage_id") is None:
                self.mark(name, "unverified", "native_unbound_scope_unclassified")
                continue
            if (
                start.get("io_scope_state") != "active"
                or start.get("stage_thread_matches") is not True
            ):
                self.mark(name, "unverified", "native_disabled_or_mismatched_scope")
            slot = self.db.execute(
                "SELECT start,end FROM slots WHERE stage=?", (start["stage_id"],)
            ).fetchone()
            if slot is None or slot[1] is None:
                self.mark(name, "unverified", "native_grant_missing")
                continue
            # Outer finished can occur after release; actual driver and close cannot.
            require(
                slot[0] <= start["at_s"] <= end["at_s"] <= closes <= slot[1], "native_outside_grant"
            )
        if not count:
            self.mark(name, "unverified", "no_native_events")

    def batches(self):
        name, count = "batch_grant_containment", 0
        fields = (
            "phase",
            "stage_id",
            "budget_id",
            "direction",
            "requested_bytes",
            "pages_per_step",
            "sleep_seconds",
            "callback_present",
        )
        for phase, transfer in self.db.execute(
            "SELECT DISTINCT phase,transfer FROM rows WHERE phase IN ('io_charge','file_batch','sqlite_batch')"
        ):
            count += 1
            start = self.one("phase=? AND transfer=? AND event='started'", (phase, transfer))
            end = self.one("phase=? AND transfer=? AND event='finished'", (phase, transfer))
            require(
                start["at_s"] <= end["at_s"] and all(start.get(k) == end.get(k) for k in fields),
                "batch_lifecycle_changed",
            )
            for row in self.rows("phase=? AND transfer=?", (phase, transfer)):
                require(
                    start["at_s"] <= row["at_s"] <= end["at_s"]
                    and all(start.get(k) == row.get(k) for k in fields),
                    "batch_event_changed",
                )
            if start["stage_id"] is None:
                self.mark(name, "unverified", "batch_unbound_scope_unclassified")
                continue
            slot = self.db.execute(
                "SELECT start,end,budget FROM slots WHERE stage=?", (start["stage_id"],)
            ).fetchone()
            if slot is None or slot[1] is None:
                self.mark(name, "unverified", "batch_grant_missing")
                continue
            require(slot[0] <= start["at_s"] <= end["at_s"] <= slot[1], "batch_outside_grant")
            if phase == "io_charge":
                require(start["budget_id"] == slot[2], "charge_budget_mismatch")
            # This check proves only containment. Exact optional reservation
            # identities are checked separately by BatchPredicates; their absence
            # never becomes byte matching, and post-commit exceptions still differ.
        if not count:
            self.mark(name, "unverified", "no_batch_events")

    def report(self):
        for name, check in (
            ("request_plan_window", self.requests),
            ("resource_lifecycles", self.releases),
            ("io_lifecycles", self.io_lifecycles),
            ("native_lifecycles", self.native_lifecycles),
            ("native_and_child_exit_window", self.execution_window),
            ("wal_associations", self.wal_associations),
            ("planned_cancel_binding", self.planned_cancel),
            ("slot_intervals", self.slots),
            ("slot_control_identity", self.control),
            ("authorized_release_durable_end", self.authorized_release_durable_end),
            ("observed_snapshot_release_order", self.observed_snapshot_release_order),
            ("native_grant_containment", self.native),
            ("batch_grant_containment", self.batches),
        ):
            self.checked(name, check)
        for name, code in (
            ("budget_bytes_and_rate", "token_observation_is_not_full_cross_stream_byte_coverage"),
            ("pressure_chain", "partial_pressure_observations_are_not_complete_transition_history"),
            (
                "snapshot_wal_limits",
                "wal_growth_subpredicate_does_not_certify_limit_decision_or_release",
            ),
            (
                "cancel_cross_flow_release_order",
                "cancel_source_and_resource_lifecycles_checked_separately_full_cross_flow_order_not_certified",
            ),
            ("borrower_seriality", "slot_interval_does_not_prove_borrower_handoff"),
            ("restart_admission", "metadata_does_not_prove_cross_controller_lock_and_exit"),
            (
                "formal_run_provenance",
                "frozen_candidate_tool_fixture_environment_and_matrix_not_certified",
            ),
            ("archive_and_scans", "original_hashes_only_no_archive_or_private_scan"),
            (
                "observer_completeness",
                "observed_capabilities_and_exit_ordinals_checked_unobserved_call_paths_not_certified",
            ),
            (
                "control_release_binding",
                "authorization_acquire_only_release_and_responsibility_termination_not_certified",
            ),
        ):
            self.mark(name, "unverified", code)
        results = {}
        for name, status, code in self.db.execute(
            "SELECT check_name,status,code FROM findings ORDER BY check_name,status,code"
        ):
            item = results.setdefault(name, {"status": status, "reasons": []})
            if status == "failed" or status == "unverified" and item["status"] == "verified":
                item["status"] = status
            item["reasons"].append(code)
        self.db.commit()
        return {
            "mechanism_acceptance": "not_evaluated",
            "scope": "observed_predicates_only",
            "source_result_preserved": self.run["result"],
            "source_phase": self.run["phase"],
            "original_sha256": self.hashes,
            "row_counts": self.counts,
            "expected_identity_fields": self.expected_fields,
            "requirements": results,
            "pressure_and_accounting": Predicates(self).report(),
            "batch_reservations": BatchPredicates(self).report(),
            "wal_growth": WalPredicates(self).report(),
            "coverage_exceptions": [
                "native_sqlite_internal_bytes_unknown",
                "plugin_io_bypassing_host_unobserved",
                "filesystem_cache_and_uninterruptible_io_not_hard_realtime",
            ],
        }


def aggregate(source, target, expected):
    index = MechanismValidation.__new__(MechanismValidation)
    try:
        MechanismValidation.__init__(index, source, target, expected)
        return index.report()
    finally:
        if getattr(index, "db", None) is not None:
            index.db.close()
