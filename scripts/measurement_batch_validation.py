"""Disk-indexed exact bindings; logical reservations never become native bytes."""


class Missing(ValueError):
    pass


class Invalid(ValueError):
    pass


def require(value, code):
    if not value:
        raise Invalid(code)


class BatchPredicates:
    def __init__(self, index):
        self.index, self.db = index, index.db
        self.consumer_checked = False
        self.consumer_duplicate = False
        self.db.executescript("""
            CREATE INDEX IF NOT EXISTS batch_fk ON rows(phase,event,json_extract(j,'$.client_charge_id'));
            CREATE INDEX IF NOT EXISTS batch_progress ON rows(phase,event,json_extract(j,'$.progress_id'));
            CREATE INDEX IF NOT EXISTS batch_request ON rows(phase,stage,event,json_extract(j,'$.charge_sequence'));
            CREATE INDEX IF NOT EXISTS batch_operation ON rows(phase,event,json_extract(j,'$.operation_id'));
            CREATE INDEX IF NOT EXISTS batch_copy ON rows(phase,event,json_extract(j,'$.copy_id'));
            CREATE INDEX IF NOT EXISTS batch_transfer ON rows(phase,event,transfer);
        """)

    def rows(self, where, args=()):
        return self.index.rows(where, args)

    def one(self, where, args=()):
        cursor = self.rows(where, args)
        row = next(cursor, None)
        if row is None:
            raise Missing("binding_missing")
        require(next(cursor, None) is None, "binding_ambiguous")
        return row

    def binding(self, event, key, value):
        if value is None:
            raise Missing("binding_identity_unknown")
        return self.one(
            "phase='batch_charge_binding' AND event=? AND json_extract(j,?)=?",
            (event, "$." + key, value),
        )

    def client(self, identifier, stage, direction, size, *, before):
        end = self.binding("request_finished", "client_charge_id", identifier)
        start = self.binding("request_started", "client_charge_id", identifier)
        mutable = {"id", "at_s", "event", "outcome"}
        require(
            all(start[k] == end[k] for k in start if k not in mutable), "client_request_changed"
        )
        require(start["id"] < end["id"] < before, "client_request_order_invalid")
        require(end["outcome"] == "ok", "batch_without_successful_reservation")
        require(end["scope_state"] == "active", "charged_batch_scope_mismatch")
        require(
            (end["stage_id"], end["direction"], end["requested_bytes"]) == (stage, direction, size),
            "client_batch_mismatch",
        )
        seq = end["charge_sequence"]
        server = self.one(
            "phase='io_charge' AND event='finished' AND stage=? "
            "AND json_extract(j,'$.charge_sequence')=?",
            (stage, seq),
        )
        begin = self.one(
            "phase='io_charge' AND event='started' AND transfer=?", (server["transfer_id"],)
        )
        for field in ("budget_id", "stage_id", "charge_sequence", "direction", "requested_bytes"):
            require(begin[field] == server[field], "server_request_changed")
        require(begin["id"] < server["id"], "server_request_order_invalid")
        require(
            (
                server["direction"],
                server["requested_bytes"],
                server["charged_bytes"],
                server["outcome"],
            )
            == (direction, size, size, "ok"),
            "server_charge_mismatch",
        )
        # A globally hashed actual stage + existing sequence identifies the request.
        # The controller/instance scope is proved by the separate control mapper.
        grants = self.one("phase='io_slot' AND event='granted' AND stage=?", (stage,))
        require(grants["budget_id"] == server["budget_id"], "server_budget_mismatch")
        require(grants["id"] < begin["id"], "charge_before_actual_grant")
        return end

    def unique_consumers(self):
        if self.consumer_checked:
            require(not self.consumer_duplicate, "reservation_consumed_twice")
            return
        duplicate = self.db.execute("""
            SELECT charge FROM (
                SELECT json_extract(j,'$.client_charge_id') AS charge FROM rows
                WHERE phase='batch_charge_binding' AND event='file_link'
                UNION ALL
                SELECT json_extract(j,'$.read_charge_id') FROM rows
                WHERE phase='batch_charge_binding' AND event='sqlite_link'
                UNION ALL
                SELECT json_extract(j,'$.write_charge_id') FROM rows
                WHERE phase='batch_charge_binding' AND event='sqlite_link'
            ) WHERE charge IS NOT NULL GROUP BY charge HAVING count(*)>1 LIMIT 1
        """).fetchone()
        self.consumer_duplicate = duplicate is not None
        self.consumer_checked = True
        require(duplicate is None, "reservation_consumed_twice")

    def file(self, row):
        self.unique_consumers()
        row = self.one(
            "phase='file_batch' AND event='finished' AND transfer=?", (row["transfer_id"],)
        )
        link = self.binding("file_link", "transfer_id", row["transfer_id"])
        operation = self.binding("file_started", "operation_id", link["operation_id"])
        completed = self.binding("file_finished", "operation_id", link["operation_id"])
        begin = self.one(
            "phase='file_batch' AND event='started' AND transfer=?", (row["transfer_id"],)
        )
        require(
            operation["id"] < link["id"] < begin["id"] < row["id"] < completed["id"],
            "file_binding_order_invalid",
        )
        require(
            (begin["stage_id"], begin["direction"], begin["requested_bytes"])
            == (row["stage_id"], row["direction"], row["requested_bytes"]),
            "file_batch_changed",
        )
        for event in (operation, completed, link):
            require(event["stage_id"] == row["stage_id"], "file_stage_changed")
        if link["scope_state"] in {"disabled", "unbound"}:
            raise Missing("unbudgeted_scope_not_exempt")
        require(link["scope_state"] == "active", "file_scope_mismatch")
        request = self.client(
            link["client_charge_id"],
            row["stage_id"],
            row["direction"],
            row["requested_bytes"],
            before=link["id"],
        )
        require(
            request["source"] == "host_file" and request["operation_id"] == link["operation_id"],
            "file_charge_source_mismatch",
        )
        require(operation["id"] < request["id"], "file_charge_outside_operation")
        if row["outcome"] != "ok" or row["actual_bytes"] is None:
            raise Missing("actual_file_bytes_unobserved_after_error")
        require(
            0 <= row["actual_bytes"] <= request["requested_bytes"] <= 256 * 1024,
            "actual_file_bytes_exceed_reservation",
        )
        if completed["outcome"] == "ok":
            require(
                row["actual_bytes"] == request["requested_bytes"], "successful_host_short_batch"
            )

    def sqlite(self, row):
        self.unique_consumers()
        link = self.binding("sqlite_link", "progress_id", row["id"])
        require(
            link["transfer_id"] == row["transfer_id"] and link["stage_id"] == row["stage_id"],
            "sqlite_progress_binding_mismatch",
        )
        require(row["id"] < link["id"], "sqlite_progress_order_invalid")
        if link["scope_state"] in {"disabled", "unbound"} or link["source"] == "unclassified":
            raise Missing("sqlite_reservation_unobserved")
        require(link["scope_state"] == "active", "sqlite_scope_mismatch")
        geometry = self.one(
            "phase='sqlite_geometry' AND event='backup_started' AND json_extract(j,'$.copy_id')=?",
            (link["copy_id"],),
        )
        require(
            geometry["stage_id"] == row["stage_id"]
            and geometry["transfer_id"] == row["transfer_id"],
            "sqlite_geometry_mismatch",
        )
        size, pages = geometry["page_size"], geometry["page_count"]
        if size is None or pages is None or link["reservation_remaining_pages"] is None:
            raise Missing("sqlite_geometry_unknown")
        remaining = link["reservation_remaining_pages"]
        require(
            0 < size <= 256 * 1024 and 0 < remaining <= pages and row["pages_per_step"] > 0,
            "sqlite_reservation_geometry_invalid",
        )
        reserved = min(row["pages_per_step"], remaining) * size
        require(reserved <= 256 * 1024, "sqlite_reservation_exceeds_chunk")
        read = self.client(
            link["read_charge_id"], row["stage_id"], "read", reserved, before=row["id"]
        )
        write = self.client(
            link["write_charge_id"], row["stage_id"], "write", reserved, before=row["id"]
        )
        for charge in (read, write):
            require(
                charge["source"] == "sqlite_reserve"
                and charge["copy_id"] == link["copy_id"]
                and charge["reservation_remaining_pages"] == remaining,
                "sqlite_charge_source_mismatch",
            )
        require(
            read["id"] < write["id"] and read["charge_sequence"] < write["charge_sequence"],
            "sqlite_reservation_order_invalid",
        )
        require(row["internal_read_bytes"] is None, "native_bytes_must_remain_unknown")
        if row["status"] not in {0, 101} or row["copied_pages"] is None:
            raise Missing("sqlite_actual_progress_unknown")
        require(
            row["total_pages"] == pages
            and remaining - row["remaining_pages"] == row["copied_pages"]
            and 0 < row["copied_pages"] * size <= reserved,
            "sqlite_progress_exceeds_reservation",
        )

    def check(self, where, function):
        count, unknown, failed = 0, 0, 0
        reasons = set()
        for row in self.rows(where):
            count += 1
            try:
                function(row)
            except Missing as exc:
                unknown += 1
                reasons.add(str(exc))
            except Invalid as exc:
                failed += 1
                reasons.add(str(exc))
        return dict(
            status="failed" if failed else "unverified" if unknown or not count else "verified",
            observed=count,
            unknown=unknown,
            contradictions=failed,
            reasons=sorted(reasons),
        )

    def report(self):
        return dict(
            mechanism_acceptance="not_evaluated",
            requirements={
                "host_file_batch_reservations": self.check(
                    "id IN (SELECT min(id) FROM rows WHERE phase='file_batch' GROUP BY transfer)",
                    self.file,
                ),
                "sqlite_logical_progress_reservations": self.check(
                    "phase='sqlite_batch' AND event='progress'", self.sqlite
                ),
                "sqlite_internal_read_bytes": dict(
                    status="unverified", reason="native_read_bytes_unknown"
                ),
                "all_heavy_io_rate_limited": dict(
                    status="unverified", reason="unobserved_bypasses_and_native_io"
                ),
            },
        )
