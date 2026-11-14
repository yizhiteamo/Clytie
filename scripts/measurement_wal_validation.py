"""Observed WAL state arithmetic only; no limit, completeness or release verdict.

The existing observer records the retained WalGrowth.last state, not every
filesystem sample. In particular an absent file after a retained zero-size WAL
leaves that state unchanged. No new observations, clock reads or product calls.
"""

if __package__:
    from . import measurement_evidence as evidence
    from . import measurement_pressure_observation as pressure
else:
    import measurement_evidence as evidence
    import measurement_pressure_observation as pressure


class WalPredicates:
    def __init__(self, index):
        self.index = index
        self.status = "verified"
        self.reasons = set()
        self.wals = self.samples = 0
        # Only the existing offline analysis database, never the source files.
        index.db.execute(
            "CREATE INDEX IF NOT EXISTS wal_growth_rows ON rows(phase,json_extract(j,'$.wal_id'),n)"
        )

    def mark(self, status, code):
        rank = {"verified": 0, "unverified": 1, "failed": 2}
        if rank[status] > rank[self.status]:
            self.status = status
        self.reasons.add(code)

    def check(self, condition, code):
        if not condition:
            self.mark("failed", code)

    def state(self, value):
        """Schema is already checked; distinguish absent WAL from unknown state."""
        if value is None:
            self.mark("unverified", "wal_state_missing")
            return False
        generation, size, page = (
            value["generation_hash"],
            value["physical_bytes"],
            value["page_size"],
        )
        if generation is None:
            self.check(size is None and page is None, "wal_absent_state_contradiction")
        elif size is None or size > 0 and page is None:
            self.mark("unverified", "wal_state_field_unknown")
            return False
        if value["additional_bytes"] is None:
            self.mark("unverified", "wal_state_field_unknown")
            return False
        return generation is None or size is not None

    def compare(self, previous, before):
        if previous is None or before is None:
            return
        # Check all known fields even when some other field is unknown. Missing
        # proof must not suppress a separate contradiction later in the stream.
        for key in pressure.WAL:
            left, right = previous[key], before[key]
            if left is not None and right is not None:
                self.check(left == right, "wal_state_discontinuous")
        if previous["additional_bytes"] is not None and before["additional_bytes"] is not None:
            # A full retained-state transition cannot happen between calls.
            if self.state(previous) and self.state(before):
                self.check(previous == before, "wal_state_discontinuous")

    def successful_sample(self, before, after, returned):
        if after is not None and returned is not None and after["additional_bytes"] is not None:
            self.check(returned == after["additional_bytes"], "wal_return_mismatch")
        if returned is None:
            self.mark("unverified", "wal_return_missing")
        before_known, after_known = self.state(before), self.state(after)
        if not before_known or not after_known:
            return
        old_generation, new_generation = before["generation_hash"], after["generation_hash"]
        old_size, new_size = before["physical_bytes"], after["physical_bytes"]
        if new_generation is None:
            # sample(None) returns without assigning self.last. A known retained
            # WAL, including size zero, therefore cannot become absent here.
            self.check(old_generation is None, "wal_retained_state_disappeared")
            increase = 0
        elif old_generation is None or new_generation != old_generation or new_size < old_size:
            increase = new_size
        else:
            increase = new_size - old_size
        if old_generation is not None and old_generation == new_generation:
            self.check(before["page_size"] == after["page_size"], "wal_generation_page_changed")
        self.check(
            after["additional_bytes"] == before["additional_bytes"] + increase,
            "wal_growth_arithmetic_mismatch",
        )

    def group(self, wal):
        initialized, sampled, previous, last_end = 0, 0, None, None
        for row in self.index.rows(
            "file='storage-phases.jsonl' AND phase='wal_growth' AND json_extract(j,'$.wal_id')=?",
            (wal,),
        ):
            # Defensive for direct Index adapters; the real Index also validates
            # every row and rejects duplicate IDs before this predicate runs.
            try:
                pressure.validate_record(row)
            except (evidence.EvidenceError, KeyError, TypeError, ValueError):
                self.mark("failed", "wal_schema_invalid")
                previous = None
                continue
            if last_end is not None:
                self.check(row["started_s"] >= last_end, "wal_calls_overlap_or_reorder")
            last_end = row["at_s"]
            before, after = row["before"], row["after"]
            if row["event"] == "initialized":
                initialized += 1
                self.check(
                    initialized == 1 and sampled == 0, "wal_initialization_duplicate_or_late"
                )
                self.check(before is None, "wal_initialization_before_present")
                self.check(
                    row["returned_additional_bytes"] is None, "wal_initialization_return_present"
                )
                if row["outcome"] == "ok":
                    self.state(after)
                    if after is not None and after["additional_bytes"] is not None:
                        self.check(after["additional_bytes"] == 0, "wal_initialization_nonzero")
            else:
                sampled += 1
                self.compare(previous, before)
                if row["outcome"] == "ok":
                    self.samples += 1
                    self.successful_sample(before, after, row["returned_additional_bytes"])
            if row["outcome"] != "ok":
                # CaptureError / cancellation / arbitrary BaseException has no
                # error-code detail here. Do not invent a successful sample or
                # require atomic state rollback for an asynchronous interruption.
                self.mark("unverified", "wal_call_not_successful")
            previous = after
        if not initialized:
            self.mark("unverified", "wal_initialization_missing")
        if not sampled:
            self.mark("unverified", "wal_sample_missing")

    def report(self):
        # Disk DISTINCT/index and one group's constant-size state; never collect
        # a complete raw stream or a per-WAL history into a Python list/map.
        for (wal,) in self.index.db.execute(
            "SELECT DISTINCT json_extract(j,'$.wal_id') FROM rows "
            "WHERE file='storage-phases.jsonl' AND phase IN ('wal_growth','wal_binding')"
        ):
            self.wals += 1
            self.group(wal)
        if not self.wals:
            self.mark("unverified", "wal_observations_missing")
        if not self.samples:
            self.mark("unverified", "wal_successful_sample_missing")
        return {
            "mechanism_acceptance": "not_evaluated",
            "scope": "recorded_wal_retained_state_and_successful_sample_arithmetic_only",
            "source_result_preserved": self.index.run["result"],
            "requirements": {
                "observed_wal_growth_arithmetic": {
                    "status": self.status,
                    "reasons": sorted(self.reasons) or ["observed_predicate_only"],
                    "wal_count": self.wals,
                    "successful_sample_count": self.samples,
                },
                "wal_sampling_completeness": {
                    "status": "unverified",
                    "reason": "unchanged_omitted_sample_not_detectable_without_call_sequence",
                },
                "view_limit_decision_and_release": {
                    "status": "unverified",
                    "reason": "no_limit_clock_result_or_cross_resource_release_proof_in_this_predicate",
                },
            },
        }
