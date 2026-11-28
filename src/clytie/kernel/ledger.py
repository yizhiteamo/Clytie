"""SQLite 原子账本；事务内没有 await，所有应用时间来自注入时钟。"""

import json
import sqlite3
from contextlib import contextmanager, nullcontext
from datetime import timedelta
from uuid import uuid4

from clytie.sdk.types import ProactiveOutcome
from clytie.storage.owner import before_storage_commit

from .projection import redact, visible

SCHEMA_VERSION = 3
EXEC_TERMINAL = {"ok", "error", "denied", "cancelled", "interrupted"}
INTENT_TERMINAL = {"sent", "failed", "unknown"}
TABLES = {"inbound", "inbound_media", "record", "invocation", "intent", "attempt", "dedupe"}
MEDIA_INDEX = """CREATE INDEX media_record ON inbound_media(json_extract(data,'$.record_id'))"""
ACTIVE_RECORD = (
    "(json_extract(data,'$.exec_status') IN ('running','pending') "
    "OR json_extract(data,'$.delivery_status')='pending')"
)
SCHEMA_SQL = (
    *(
        f'CREATE TABLE "{table}" (id TEXT PRIMARY KEY, data TEXT NOT NULL)'
        for table in sorted(TABLES)
    ),
    "CREATE TABLE audit (id INTEGER PRIMARY KEY, data TEXT NOT NULL)",
    "CREATE UNIQUE INDEX task_id ON \"record\"(json_extract(data,'$.task_id'))",
    "CREATE UNIQUE INDEX source_event ON \"record\"(json_extract(data,'$.source_event_id'))",
    "CREATE UNIQUE INDEX invocation_key ON invocation(json_extract(data,'$.idem_key'))",
    MEDIA_INDEX,
)
HEALTH_SQL = (
    "CREATE INDEX IF NOT EXISTS active_conversation ON record("
    "json_extract(data,'$.conversation_id')) WHERE " + ACTIVE_RECORD,
    "CREATE INDEX IF NOT EXISTS proactive_calls_by_day ON invocation("
    "json_extract(data,'$.proactive_day'),json_extract(data,'$.tool')) "
    "WHERE json_extract(data,'$.proactive_day') IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS plugin_usage_by_day ON audit("
    "json_extract(data,'$.day'), json_extract(data,'$.plugin')) "
    "WHERE json_extract(data,'$.kind')='model.usage' AND json_extract(data,'$.purpose')='plugin'",
    "CREATE INDEX IF NOT EXISTS health_tasks ON record("
    "json_extract(data,'$.kind'),json_extract(data,'$.exec_status'))",
    "CREATE INDEX IF NOT EXISTS health_sending ON intent(json_extract(data,'$.status'))",
    "CREATE INDEX IF NOT EXISTS group_sent ON intent("
    "json_extract(data,'$.conversation_id'),json_extract(data,'$.error_written_at')) "
    "WHERE json_extract(data,'$.kind')='reply' AND json_extract(data,'$.status')='sent'",
    "CREATE INDEX IF NOT EXISTS intent_trace ON intent(json_extract(data,'$.trace_id'))",
    "CREATE INDEX IF NOT EXISTS child_trace ON record(json_extract(data,'$.parent_trace_id'))",
    "CREATE INDEX IF NOT EXISTS belt_trips ON audit(json_extract(data,'$.conversation_id'),id) "
    "WHERE json_extract(data,'$.kind')='safety_belt.tripped'",
    "CREATE INDEX IF NOT EXISTS attention_day ON audit("
    "json_extract(data,'$.day'),json_extract(data,'$.conversation_id')) "
    "WHERE json_extract(data,'$.purpose')='attention'",
    "CREATE INDEX IF NOT EXISTS sent_message_id ON attempt("
    "json_extract(data,'$.receipt.platform_message_id')) "
    "WHERE json_extract(data,'$.receipt.status')='sent'",
)


class LedgerError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def dump(value):
    return value.model_dump(mode="json")


class Ledger:
    """运行期间由上层持有 ledger/.lock；只读实例不修正或创建任何文件。"""

    def __init__(self, path, companion_id, clock, *, readonly=False, coordination=None):
        self.path, self.companion_id, self.clock = path, companion_id, clock
        self.readonly = readonly
        self.coordination = coordination
        if readonly and (path.parent / ".migration.json").exists():
            raise LedgerError("ledger_unavailable", "迁移中或待迁移")
        if readonly and not path.is_file():
            raise LedgerError("ledger_missing", "ledger does not exist")
        if not readonly:
            path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(
            path.as_uri() + ("?mode=ro" if readonly else "?mode=rwc"), uri=True
        )
        try:
            version = self.db.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise LedgerError("schema_too_new", "ledger schema newer than code")
            if readonly and version < SCHEMA_VERSION:
                raise LedgerError("ledger_unavailable", "迁移中或待迁移")
            if not readonly and 0 < version < SCHEMA_VERSION:
                raise LedgerError("ledger_unavailable", "migration requires LedgerRuntime")
            if not readonly:
                if self.db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                    raise LedgerError("ledger_unavailable", "WAL unavailable")
            if not readonly and version == 0:
                with self.db:
                    for statement in SCHEMA_SQL:
                        self.db.execute(statement)
                    self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            if not readonly:
                with self.db:
                    for statement in HEALTH_SQL:
                        self.db.execute(statement)
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def now(self):
        return self.clock.now().isoformat()

    @contextmanager
    def transaction(self):
        if self.readonly:
            raise LedgerError("read_only", "ledger is read-only")
        with self.coordination.commit() if self.coordination else nullcontext(), self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            before_storage_commit()

    def get(self, table, key):
        assert table in TABLES
        row = self.db.execute(f'SELECT data FROM "{table}" WHERE id=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def rows(self, table):
        assert table in TABLES | {"audit"}
        return [
            json.loads(r[0]) for r in self.db.execute(f'SELECT data FROM "{table}" ORDER BY rowid')
        ]

    def health_counts(self):
        terminal = sorted(EXEC_TERMINAL)
        marks = ",".join("?" for _ in terminal)
        tasks = self.db.execute(
            "SELECT count(*) FROM record WHERE json_extract(data,'$.kind')='task' "
            f"AND json_extract(data,'$.exec_status') NOT IN ({marks})",
            terminal,
        ).fetchone()[0]
        sending = self.db.execute(
            "SELECT count(*) FROM intent WHERE json_extract(data,'$.status')='sending'"
        ).fetchone()[0]
        return dict(inflight_tasks=tasks, sending=sending)

    def put(self, table, key, value):
        assert table in TABLES
        self.db.execute(
            f'INSERT INTO "{table}" VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
            (key, encode(value)),
        )

    def audit(self, kind, *, trace_id=None, **data):
        self.db.execute(
            "INSERT INTO audit(data) VALUES (?)",
            (encode(dict(kind=kind, trace_id=trace_id, at=self.now(), **data)),),
        )

    def claim_inbound(self, event, *, historical=False):
        if event.companion_id != self.companion_id or not event.dedupable:
            raise ValueError("wrong companion or non-dedupable event")
        key = encode(
            [self.companion_id, event.channel_id, event.event_id[len(event.channel_id) + 1 :]]
        )
        with self.transaction():
            existing = self.get("dedupe", key)
            if existing:
                return existing["event_id"], False
            self.put("dedupe", key, dict(event_id=event.event_id, first_seen_at=self.now()))
            data = dump(event)
            if historical:
                data["historical"] = True
            data["content_written_at"] = self.now()
            data.update(
                state="received",
                scene=event.conversation.scene,
                conversation_id=event.conversation.conversation_id,
            )
            self.put("inbound", event.event_id, data)
            if event.message and event.message.sender.role == "owner":
                self._supersede_proactive(event.conversation.conversation_id)
        return event.event_id, True

    def _supersede_proactive(self, conversation_id):
        # The inbound claim, capability start, and first send attempt are ordered
        # by the ledger writer. Once execution or transmission starts, let it finish.
        row = self.db.execute(
            "SELECT data FROM record WHERE "
            + ACTIVE_RECORD
            + " AND json_extract(data,'$.conversation_id')=? AND json_extract(data,'$.proactive')=1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return
        record = json.loads(row[0])
        trace = record["trace_id"]
        if (
            record.get("proactive_result")
            or record.get("started_at")
            or self.db.execute(
                "SELECT 1 FROM intent WHERE json_extract(data,'$.trace_id')=? "
                "AND json_extract(data,'$.attempts')>0 LIMIT 1",
                (trace,),
            ).fetchone()
        ):
            return
        record.update(
            proactive_result="superseded",
            cancelled=True,
            exec_status="cancelled",
            finished_at=self.now(),
            error={"code": "superseded", "message": ""},
        )
        self.put("record", trace, record)
        self.audit("proactive.superseded", trace_id=trace)

    def proactive_result(self, plugin_id, trace):
        record = self.get("record", trace) if isinstance(trace, str) else None
        if not plugin_id or not record or record.get("proactive_plugin") != plugin_id:
            return ProactiveOutcome(status="not_found")
        if first_sent := record.get("proactive_sent_at"):
            return ProactiveOutcome(status="sent", sent_at=first_sent)
        delivery = record["delivery_status"]
        if delivery == "unknown":
            return ProactiveOutcome(status="unknown")
        if outcome := record.get("proactive_result"):
            return ProactiveOutcome(status=outcome)
        if record["exec_status"] == "interrupted":
            return ProactiveOutcome(status="interrupted")
        if record["exec_status"] not in EXEC_TERMINAL or delivery == "pending":
            return ProactiveOutcome(status="pending")
        return ProactiveOutcome(status="failed")

    def update_media(self, record_id, part_index, status, asset=None, *, first_frame=False):
        with self.transaction():
            event = self.get("inbound", record_id)
            if not event or not (event.get("message") or {}).get("parts"):
                return False
            key = encode([record_id, part_index])
            self.put(
                "inbound_media",
                key,
                dict(
                    record_id=record_id,
                    part_index=part_index,
                    asset_id=asset.asset_id if asset else None,
                    asset=dump(asset) if asset else None,
                    status=status,
                    fetched_at=self.now() if status != "pending" else None,
                    first_frame=first_frame,
                ),
            )
            return True

    def record_opened_since(self, conversation_id, watermark):
        return (
            self.db.execute(
                """SELECT 1 FROM record WHERE rowid>? AND json_extract(data,'$.conversation_id')=? LIMIT 1""",
                (watermark, conversation_id),
            ).fetchone()
            is not None
        )

    def prepare_watermark(self, conversation_id):
        """Capture the start of a draft only while no chat or task is in flight."""
        busy = self.db.execute(
            """SELECT 1 FROM record WHERE json_extract(data,'$.kind') IN ('chat','task')
               AND json_extract(data,'$.exec_status') IN ('running','pending')
               AND json_extract(data,'$.conversation_id')=? LIMIT 1""",
            (conversation_id,),
        ).fetchone()
        if busy:
            return None
        return self.db.execute("SELECT coalesce(max(rowid),0) FROM record").fetchone()[0]

    def open_record(
        self,
        *,
        event_id=None,
        invocation=None,
        parent_trace_id=None,
        conversation=None,
        prepared_trace=None,
        watermark=None,
        partial_groups=(),
        proactive=False,
        proactive_plugin=None,
    ):
        with self.transaction():
            event = self.get("inbound", event_id) if event_id else None
            if event_id and event is None:
                raise ValueError("unknown inbound")
            if event and event["state"] != "received":
                raise ValueError("inbound already consumed")
            if not event and invocation is None and conversation is None:
                raise ValueError("explicit invocation required without inbound")
            source = (
                event["conversation"] if event else dump(conversation or invocation.conversation)
            )
            if (
                proactive
                and self.db.execute(
                    "SELECT 1 FROM record WHERE "
                    + ACTIVE_RECORD
                    + " AND json_extract(data,'$.conversation_id')=? LIMIT 1",
                    (source["conversation_id"],),
                ).fetchone()
            ):
                return None
            actor = (
                event["message"]["sender"]
                if event and event.get("message")
                else (
                    dump(invocation.actor)
                    if invocation and invocation.actor and not proactive
                    else None
                )
            )
            if prepared_trace and self.record_opened_since(source["conversation_id"], watermark):
                prepared_trace = None
            trace = invocation.trace_id if invocation else prepared_trace or uuid4().hex
            record = dict(
                trace_id=trace,
                companion_id=self.companion_id,
                source_event_id=event_id,
                conversation_id=source["conversation_id"],
                channel_id=source["channel_id"],
                scene=source["scene"],
                requester=actor,
                kind="chat",
                task_id=None,
                tool=None,
                parent_trace_id=parent_trace_id,
                exec_status="pending",
                delivery_status="none",
                deadline=None,
                created_at=self.now(),
                started_at=None,
                finished_at=None,
                cancelled=False,
                notified=False,
                error=None,
            )
            if proactive:
                record["proactive"] = True
                record["proactive_plugin"] = proactive_plugin
            if prepared_trace:
                record["model_calls"] = 1
                for group in partial_groups:
                    self.audit("partial_group_context", group_id=group)
            if self.get("record", trace):
                raise ValueError("duplicate trace")
            self.put("record", trace, record)
            if event:
                event["state"] = "processing"
                self.put("inbound", event_id, event)
        return record

    def ignore(self, event_id, reason=None):
        with self.transaction():
            event = self.get("inbound", event_id)
            if event["state"] != "received":
                raise ValueError("inbound already consumed")
            event["state"] = "ignored"
            event["reason"] = reason
            self.put("inbound", event_id, event)

    def merge(self, event_ids, trace):
        with self.transaction():
            for event_id in event_ids:
                event = self.get("inbound", event_id)
                if event["state"] != "received":
                    raise ValueError("inbound already consumed")
                event.update(state="merged", merged_into=trace)
                self.put("inbound", event_id, event)

    def find_invocation(self, key):
        encoded = encode(key)
        return next((r for r in self.rows("invocation") if r["idem_key"] == encoded), None)

    def proactive_counts(self):
        day = self.clock.now().astimezone(self.clock.local_tz).date().isoformat()
        return dict(
            self.db.execute(
                "SELECT json_extract(data,'$.tool'), count(*) FROM invocation "
                "WHERE json_extract(data,'$.proactive_day')=? GROUP BY json_extract(data,'$.tool')",
                (day,),
            )
        )

    def claim_invocation(
        self,
        trace,
        plugin_id,
        call,
        plugin_key,
        *,
        timeout,
        schema,
        proactive_limit=None,
        light=False,
    ):
        with self.transaction():
            r = self.get("record", trace)
            key = [
                self.companion_id,
                plugin_id,
                call.tool,
                r["source_event_id"] or trace,
                plugin_key,
            ]
            found = self.find_invocation(key)
            if found:
                return found, False
            if r["exec_status"] in EXEC_TERMINAL or r["cancelled"]:
                raise ValueError("record closed")
            proactive_day = None
            if proactive_limit is not None:
                proactive_day = self.clock.now().astimezone(self.clock.local_tz).date().isoformat()
                used = self.db.execute(
                    "SELECT count(*) FROM invocation WHERE json_extract(data,'$.proactive_day')=? "
                    "AND json_extract(data,'$.tool')=?",
                    (proactive_day, call.tool),
                ).fetchone()[0]
                if used >= proactive_limit:
                    return None, False
            first = not light and r["kind"] == "chat"
            if first:
                ids = {x["task_id"] for x in self.rows("record")}
                task_id = uuid4().hex[:8]
                while task_id in ids:
                    task_id = uuid4().hex[:8]
                r.update(
                    kind="task",
                    task_id=task_id,
                    tool=call.tool,
                    deadline=(self.clock.now() + timedelta(seconds=timeout)).isoformat(),
                    claimed_at=self.now(),
                )
            r["exec_status"] = "running"
            self.put("record", trace, r)
            invocation = dict(
                invocation_id=uuid4().hex,
                origin="proactive" if r.get("proactive") else "message",
                trace_id=trace,
                plugin_id=plugin_id,
                tool=call.tool,
                idem_key=encode(key),
                call_id=call.call_id,
                args=redact(dump(call)["arguments"], schema),
                status="pending",
                result=None,
                error=None,
                asset_ids=[],
                claimed_at=self.now(),
                started_at=None,
                finished_at=None,
            )
            if proactive_day is not None:
                invocation["proactive_day"] = proactive_day
            if light:
                self.audit(
                    "tool.light",
                    trace_id=trace,
                    tool=call.tool,
                    invocation_id=invocation["invocation_id"],
                )
            if r.get("proactive"):
                self.audit("proactive.tool", trace_id=trace, tool=call.tool, origin="proactive")
            self.put("invocation", invocation["invocation_id"], invocation)
        return invocation, first

    def start_invocation(self, invocation_id):
        with self.transaction():
            inv = self.get("invocation", invocation_id)
            if inv["status"] != "pending":
                return False
            r = self.get("record", inv["trace_id"])
            if r.get("proactive_result") == "superseded":
                inv.update(status="cancelled", finished_at=self.now())
                self.put("invocation", invocation_id, inv)
                return False
            inv.update(status="running", started_at=self.now())
            self.put("invocation", invocation_id, inv)
            r["started_at"] = r["started_at"] or self.now()
            self.put("record", r["trace_id"], r)
        return True

    def finish_invocation(self, invocation_id, result):
        with self.transaction():
            inv = self.get("invocation", invocation_id)
            if inv["status"] in EXEC_TERMINAL:
                self.audit("late_result", trace_id=inv["trace_id"], content=dump(result))
                return False
            inv.update(
                status=result.status,
                result=dump(result),
                error=dump(result.error) if result.error else None,
                asset_ids=[a.asset_id for a in result.assets],
                finished_at=self.now(),
            )
            self.put("invocation", invocation_id, inv)
        return True

    def _register(self, r, intents, *, reaction=None):
        if (r["exec_status"] in EXEC_TERMINAL or r["cancelled"]) and reaction is None:
            raise ValueError("record closed")
        for intent in intents:
            if intent.trace_id != r["trace_id"] or intent.companion_id != self.companion_id:
                raise ValueError("intent record mismatch")
            if self.get("intent", intent.intent_id):
                raise ValueError("duplicate intent")
            if reaction is not None and intent.kind != "react":
                raise ValueError("status reaction must be react")
            data = dump(intent)
            data.update(
                status="pending",
                status_reaction=reaction is not None,
                reaction_state=reaction,
                created_at=self.now(),
                error=None,
                attempts=0,
            )
            self.put("intent", intent.intent_id, data)

    def register_intents(self, trace, intents, *, reaction=None):
        with self.transaction():
            r = self.get("record", trace)
            self._register(r, intents, reaction=reaction)
            self._aggregate(trace)

    def seal(self, trace, status, intents=(), error=None):
        if status not in EXEC_TERMINAL:
            raise ValueError("execution must end in terminal state")
        with self.transaction():
            r = self.get("record", trace)
            if r["exec_status"] in EXEC_TERMINAL:
                self.audit("late_execution", trace_id=trace, status=status)
                return False
            if status == "cancelled":
                r["cancelled"] = True
            if not r["cancelled"]:
                self._register(r, intents)
            r.update(exec_status=status, finished_at=self.now(), error=error)
            self.put("record", trace, r)
            self._aggregate(trace)
        return True

    def intents(self, trace):
        return [r for r in self.rows("intent") if r["trace_id"] == trace]

    def _aggregate(self, trace):
        values = [r["status"] for r in self.intents(trace) if not r["status_reaction"]]
        status = (
            "none"
            if not values
            else "pending"
            if any(v not in INTENT_TERMINAL for v in values)
            else "sent"
            if all(v == "sent" for v in values)
            else "unknown"
            if "unknown" in values
            else "partial"
            if "sent" in values
            else "failed"
        )
        r = self.get("record", trace)
        r["delivery_status"] = status
        self.put("record", trace, r)

    def begin_attempt(self, intent_id):
        with self.transaction():
            intent = self.get("intent", intent_id)
            if intent["status"] not in {"pending", "retry_wait"}:
                raise ValueError("intent cannot be attempted")
            n = intent["attempts"] + 1
            intent.update(status="sending", attempts=n)
            self.put("intent", intent_id, intent)
            self.put(
                "attempt",
                encode([intent_id, n]),
                dict(
                    intent_id=intent_id, n=n, status="sending", started_at=self.now(), receipt=None
                ),
            )
            self._aggregate(intent["trace_id"])
        return n

    def record_receipt(self, intent_id, n, receipt, *, retry=False, on_confirmed=None):
        with self.transaction():
            key = encode([intent_id, n])
            attempt = self.get("attempt", key)
            intent = self.get("intent", intent_id)
            if attempt is None:
                raise ValueError("unknown attempt")
            value = dump(receipt)
            late = attempt["status"] == "unknown"
            if receipt.intent_id != intent_id:
                self.audit("mismatched_receipt", trace_id=intent["trace_id"], intent_id=intent_id)
                if attempt["status"] != "sending":
                    return False
                value = None
                status = "unknown"
            else:
                status = receipt.status
            if attempt["receipt"] is not None and not (late and status in {"sent", "failed"}):
                if attempt["receipt"] != value:
                    self.audit("conflicting_receipt", trace_id=intent["trace_id"], content=value)
                return False
            if attempt["status"] in {"sent", "failed"}:
                return False
            attempt.update(status=status, receipt=value, receipt_written_at=self.now())
            self.put("attempt", key, attempt)
            if n == intent["attempts"]:
                intent["status"] = (
                    "retry_wait"
                    if retry and not late and not intent["status_reaction"] and status == "failed"
                    else status
                )
                intent["error"] = (
                    value.get("error") if value else {"code": "mismatched_receipt", "message": ""}
                )
                intent["error_written_at"] = self.now()
                self.put("intent", intent_id, intent)
                if status == "sent" and not intent["status_reaction"]:
                    record = self.get("record", intent["trace_id"])
                    if record.get("proactive") and not record.get("proactive_sent_at"):
                        record["proactive_sent_at"] = value["at"]
                        self.put("record", record["trace_id"], record)
                self._aggregate(intent["trace_id"])
                if status == "sent" and on_confirmed:
                    on_confirmed(intent)
            if late:
                self.audit("late_receipt", trace_id=intent["trace_id"], content=value)
            return not late

    def fail_intent(self, intent_id, code):
        with self.transaction():
            intent = self.get("intent", intent_id)
            if intent["status"] not in {"pending", "retry_wait"}:
                return
            intent.update(status="failed", error={"code": code, "message": ""})
            self.put("intent", intent_id, intent)
            self._aggregate(intent["trace_id"])

    def restart(self):
        interrupted = []
        with self.transaction():
            self.db.execute(
                """UPDATE inbound_media SET data=json_set(data,'$.status','failed','$.fetched_at',?)
                   WHERE json_extract(data,'$.status')='pending'""",
                (self.now(),),
            )
            for r in self.rows("record"):
                r["notified"] = True
                self.put("record", r["trace_id"], r)
                if r["exec_status"] not in EXEC_TERMINAL:
                    r.update(
                        exec_status="interrupted",
                        finished_at=self.now(),
                        error={"code": "interrupted", "message": ""},
                    )
                    self.put("record", r["trace_id"], r)
                    self.audit("restart_interrupted", trace_id=r["trace_id"])
                    if r["kind"] == "task":
                        interrupted.append(r["trace_id"])
            for inv in self.rows("invocation"):
                if inv["status"] not in EXEC_TERMINAL:
                    inv.update(status="interrupted", finished_at=self.now())
                    self.put("invocation", inv["invocation_id"], inv)
            for a in self.rows("attempt"):
                if a["status"] == "sending":
                    a["status"] = "unknown"
                    self.put("attempt", encode([a["intent_id"], a["n"]]), a)
            for i in self.rows("intent"):
                if i["status"] not in INTENT_TERMINAL:
                    i.update(
                        status="unknown" if i["status"] == "sending" else "failed",
                        error={
                            "code": "receipt_unknown_on_restart"
                            if i["status"] == "sending"
                            else "not_sent_before_restart",
                            "message": "",
                        },
                    )
                    self.put("intent", i["intent_id"], i)
                    self.audit(
                        "restart_delivery",
                        trace_id=i["trace_id"],
                        intent_id=i["intent_id"],
                        status=i["status"],
                    )
                    self._aggregate(i["trace_id"])
            for e in self.rows("inbound"):
                if e["state"] == "received":
                    e["state"] = "dropped_on_restart"
                    self.put("inbound", e["event_id"], e)
                    self.audit("restart_inbound_dropped", event_id=e["event_id"])
        return interrupted

    def project(self, *, scene, conversation_id, private_scenes=("owner_private",)):
        return [
            r
            for r in self.rows("record")
            if visible(
                r, scene=scene, conversation_id=conversation_id, private_scenes=private_scenes
            )
        ]

    def expire_content(self, days):
        if days is None:
            return
        from datetime import datetime

        cutoff = self.clock.now() - timedelta(days=days)

        def expired(value):
            return value and datetime.fromisoformat(value) <= cutoff

        changed = 0
        with self.transaction():
            for event in self.rows("inbound"):
                if (
                    expired(event.get("content_written_at"))
                    and event.get("message")
                    and event["message"]["parts"] is not None
                ):
                    event["message"]["parts"] = None
                    self.put("inbound", event["event_id"], event)
                    self.db.execute(
                        "DELETE FROM inbound_media WHERE json_extract(data,'$.record_id')=?",
                        (event["event_id"],),
                    )
                    changed += 1
            for intent in self.rows("intent"):
                dirty = False
                if expired(intent["created_at"]) and intent["parts"] is not None:
                    intent["parts"] = None
                    dirty = True
                if (
                    expired(intent.get("error_written_at"))
                    and intent["error"]
                    and intent["error"].get("message") is not None
                ):
                    intent["error"]["message"] = None
                    dirty = True
                if dirty:
                    self.put("intent", intent["intent_id"], intent)
                    changed += 1
            for inv in self.rows("invocation"):
                dirty = False
                if expired(inv["claimed_at"]) and inv.get("plan") is not None:
                    inv["plan"], dirty = None, True
                if expired(inv["claimed_at"]) and inv["args"] is not None:
                    inv["args"], dirty = None, True
                if expired(inv["finished_at"]) and inv["result"] is not None:
                    inv["result"], dirty = None, True
                    if inv["error"]:
                        inv["error"]["message"] = None
                if dirty:
                    self.put("invocation", inv["invocation_id"], inv)
                    changed += 1
            for attempt in self.rows("attempt"):
                receipt = attempt["receipt"]
                if (
                    expired(attempt.get("receipt_written_at"))
                    and receipt
                    and receipt.get("error")
                    and receipt["error"].get("message") is not None
                ):
                    receipt["error"]["message"] = None
                    self.put("attempt", encode([attempt["intent_id"], attempt["n"]]), attempt)
                    changed += 1
            for key, raw in self.db.execute("SELECT id,data FROM audit").fetchall():
                audit = json.loads(raw)
                if not expired(audit["at"]) or audit.get("content") is None:
                    continue
                if audit["kind"] in {"late_result", "model.late_result"}:
                    audit["content"] = None
                elif (
                    audit["kind"] in {"late_receipt", "conflicting_receipt"}
                    and audit["content"].get("error")
                    and audit["content"]["error"].get("message") is not None
                ):
                    audit["content"]["error"]["message"] = None
                else:
                    continue
                self.db.execute("UPDATE audit SET data=? WHERE id=?", (encode(audit), key))
                changed += 1
            if changed:
                self.audit("content_expired", count=changed)
