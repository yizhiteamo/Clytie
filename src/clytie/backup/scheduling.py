"""持久调度与请求认领；执行、核验和通知由管理工作进程负责。"""

from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from datetime import timezone as utc_timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from clytie.plugin_host.locking import file_lock
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical, contained, identifier

IMPORTANT_CHANGES = frozenset({"fact", "revision", "retraction", "import", "manual_restore"})
UTC = utc_timezone.utc


@dataclass(frozen=True)
class BackupOperationRequest:
    operation_id: str
    trigger_id: str
    trigger: str
    scope: str
    companion_id: str | None
    manual: bool
    attempt: int
    requested_versions: dict[str, int]


class BackupScheduler:
    def __init__(self, home, *, clock, timezone="UTC", config=None):
        self.home = Path(home)
        self.clock = clock
        self.timezone = ZoneInfo(timezone)
        self.config = copy.deepcopy(config or {})
        self._started = False
        self.path = contained(self.home, self.home / "supervisor/backup-scheduler.json")
        self.lock_path = contained(self.home, self.home / "supervisor/backup-scheduler.lock")

    @contextmanager
    def _state(self):
        with file_lock(self.lock_path, {"operation": "backup_schedule"}):
            contained(self.home, self.path)
            if self.path.exists():
                state = json.loads(atomic_read(self.path))
                if state.get("schema_version") != 1:
                    raise ValueError("scheduler_state_incompatible")
            else:
                state = {
                    "schema_version": 1,
                    "dirty": {},
                    "versions": {},
                    "blocked": {},
                    "jobs": {},
                    "active": None,
                    "shutdowns": [],
                    "last_poll": None,
                    "schedule_dates": [],
                }
            yield state
            atomic_write(self.path, canonical(state))

    def _now(self):
        value = self.clock.now()
        if value.tzinfo is None:
            raise ValueError("clock_requires_timezone")
        return value.astimezone(UTC)

    def status(self):
        with self._state() as state:
            return copy.deepcopy(state)

    def mark_dirty(self, companion_id, revision, kind):
        identifier(companion_id)
        if type(revision) is not int or revision < 1:
            raise ValueError("invalid_revision")
        if kind not in IMPORTANT_CHANGES:
            return False
        with self._state() as state:
            old = state["dirty"].get(companion_id)
            if revision <= state["versions"].get(companion_id, 0):
                return False
            if revision <= state["blocked"].get(companion_id, 0):
                return False
            delay = self.config.get("memory", {}).get("debounce_seconds", 300)
            state["dirty"][companion_id] = {
                "revision": revision,
                "due": old["due"] if old else (self._now() + timedelta(seconds=delay)).isoformat(),
            }
            state["versions"][companion_id] = revision
        return True

    def _enqueue(self, state, key, trigger, scope, companion_id=None, *, manual=False, merge=True):
        if key in state["jobs"]:
            return False
        # One waiting automatic request per scope/group; active requests stay immutable.
        if (
            merge
            and not manual
            and any(
                j["status"] == "pending"
                and not j["manual"]
                and j["scope"] == scope
                and j["companion_id"] == companion_id
                for j in state["jobs"].values()
            )
        ):
            return False
        versions = {
            cid: row["revision"]
            for cid, row in state["dirty"].items()
            if scope == "instance" or cid == companion_id
        }
        state["jobs"][key] = {
            "trigger_id": key,
            "trigger": trigger,
            "scope": scope,
            "companion_id": companion_id,
            "manual": manual,
            "attempt": 0,
            "requested_versions": versions,
            "status": "pending",
            "due": self._now().isoformat(),
        }
        return True

    def manual(self, operation_id, scope, companion_id=None):
        if (
            not isinstance(operation_id, str)
            or not operation_id
            or scope not in {"memory", "instance"}
        ):
            raise ValueError("invalid_manual_request")
        if scope == "memory":
            identifier(companion_id)
        elif companion_id is not None:
            raise ValueError("instance_request_has_companion")
        with self._state() as state:
            key = "manual:" + operation_id
            existing = state["jobs"].get(key)
            if existing and (existing["scope"], existing["companion_id"]) != (scope, companion_id):
                raise ValueError("operation_identity_conflict")
            return self._enqueue(state, key, "manual", scope, companion_id, manual=True)

    def _automatic(self, state):
        if not self.config.get("enabled", True):
            return
        self._schedule(state)
        if not self.config.get("memory", {}).get("enabled", True):
            return
        for cid, row in state["dirty"].items():
            if datetime.fromisoformat(row["due"]) > self._now():
                continue
            if row["revision"] <= state["blocked"].get(cid, 0):
                continue
            if any(
                j["status"] in {"pending", "active"}
                and j["scope"] == "memory"
                and j["companion_id"] == cid
                for j in state["jobs"].values()
            ):
                continue
            self._enqueue(state, f"memory:{cid}:{row['revision']}", "memory", "memory", cid)

    def _schedule(self, state):
        schedule = self.config.get("schedule", {})
        now = self._now()
        local_date = now.astimezone(self.timezone).date()
        hour, minute = map(int, schedule.get("at", "03:00").split(":"))
        days = schedule.get("days", [])
        last = datetime.fromisoformat(state["last_poll"]) if state["last_poll"] else None
        candidate = None
        for offset in range(8):
            day = local_date - timedelta(days=offset)
            if days and day.isoweekday() not in days:
                continue
            wall = datetime(day.year, day.month, day.day, hour, minute)
            while wall.date() == day:
                possible = []
                for fold in (0, 1):
                    instant = wall.replace(tzinfo=self.timezone, fold=fold).astimezone(UTC)
                    if instant.astimezone(self.timezone).replace(tzinfo=None) == wall:
                        possible.append(instant)
                if possible:
                    due = min(possible)
                    break
                wall += timedelta(minutes=1)
            else:
                continue
            if due <= now and (last is not None or day == local_date):
                candidate = (day.isoformat(), due)
                break
        if candidate:
            day, due = candidate
            schedule_id = hashlib.sha256(
                canonical(
                    {
                        "timezone": str(self.timezone),
                        "at": schedule.get("at", "03:00"),
                        "days": days,
                    }
                )
            ).hexdigest()[:16]
            key = f"schedule:{schedule_id}:{day}"
            if key not in state["schedule_dates"]:
                state["schedule_dates"].append(key)
                catching_up = not self._started and due < now
                if (last is None or due > last) and (
                    not catching_up or schedule.get("catch_up", True)
                ):
                    self._enqueue(state, key, "schedule", "instance")
        state["last_poll"] = now.isoformat()
        self._started = True

    def poll(self, *, ready=True, maintenance=False):
        if maintenance or not ready:
            return None
        with self._state() as state:
            self._automatic(state)
            if state["active"]:
                return None
            for key, job in state["jobs"].items():
                if job["status"] != "pending" or datetime.fromisoformat(job["due"]) > self._now():
                    continue
                if not job["manual"] and not self.config.get("enabled", True):
                    continue
                if (
                    not job["manual"]
                    and job["scope"] == "memory"
                    and not self.config.get("memory", {}).get("enabled", True)
                ):
                    continue
                job["attempt"] += 1
                job["status"] = "active"
                job["requested_versions"] = {
                    cid: row["revision"]
                    for cid, row in state["dirty"].items()
                    if job["scope"] == "instance" or cid == job["companion_id"]
                }
                operation_id = (
                    key[len("manual:") :]
                    if job["manual"]
                    else hashlib.sha256(f"{key}:{job['attempt']}".encode()).hexdigest()[:32]
                )
                job["operation_id"] = operation_id
                state["active"] = key
                return BackupOperationRequest(
                    **{
                        name: copy.deepcopy(job[name])
                        for name in BackupOperationRequest.__dataclass_fields__
                    }
                )
        return None

    def finish(
        self,
        operation_id,
        *,
        creation,
        covered_versions=None,
        verified=True,
        committed=False,
        retry_allowed=True,
    ):
        if creation not in {"complete", "partial", "failed", "cancelled", "unknown"}:
            raise ValueError("invalid_creation_result")
        result = {
            "creation": creation,
            "covered_versions": covered_versions or {},
            "verified": verified,
            "committed": committed,
        }
        with self._state() as state:
            for previous in state["jobs"].values():
                for record in previous.get("attempts", []):
                    if record["operation_id"] == operation_id:
                        if record["result"] != result:
                            raise ValueError("operation_result_conflict")
                        return
            key = state["active"]
            if not key or state["jobs"][key].get("operation_id") != operation_id:
                raise ValueError("operation_not_active")
            job = state["jobs"][key]
            if creation == "unknown":
                job["status"] = "unknown"
                job["verification"] = "pending"
                return
            job["status"] = creation
            job["creation"] = creation
            job["verification"] = "passed" if verified else "failed"
            job["completed_at"] = self._now().isoformat()
            job["covered_versions"] = copy.deepcopy(covered_versions or {})
            job.setdefault("attempts", []).append(
                {
                    "attempt": job["attempt"],
                    "operation_id": operation_id,
                    "result": copy.deepcopy(result),
                    "confirmed_at": self._now().isoformat(),
                }
            )
            state["active"] = None
            covered = covered_versions or {}
            if creation == "complete" and verified:
                for cid, revision in covered.items():
                    if type(revision) is not int or revision < 1:
                        raise ValueError("invalid_covered_revision")
                    if job["scope"] == "memory" and cid != job["companion_id"]:
                        raise ValueError("coverage_outside_scope")
                    if cid in state["dirty"] and state["dirty"][cid]["revision"] <= revision:
                        del state["dirty"][cid]
                for pending in state["jobs"].values():
                    if (
                        pending["status"] == "pending"
                        and not pending["manual"]
                        and pending["scope"] == "memory"
                        and pending["companion_id"] not in state["dirty"]
                    ):
                        pending["status"] = "covered"
            elif job["scope"] == "memory" and not job["manual"]:
                for cid, revision in job["requested_versions"].items():
                    state["blocked"][cid] = max(state["blocked"].get(cid, 0), revision)
            if creation == "failed" and not committed and retry_allowed and not job["manual"]:
                delays = (600, 1800, 3600)
                if job["attempt"] <= len(delays):
                    job["due"] = (
                        self._now() + timedelta(seconds=delays[job["attempt"] - 1])
                    ).isoformat()
                    job["status"] = "pending"
                else:
                    job["status"] = "exhausted"
            elif committed:
                job["status"] = "committed"

    def recover_interrupted(self, operation_id):
        """只在外层已核验执行者结束和产物后调用；不重放该请求。"""
        self.finish(operation_id, creation="failed", verified=False, retry_allowed=False)

    def shutdown(self, shutdown_id):
        if not isinstance(shutdown_id, str) or not shutdown_id:
            raise ValueError("invalid_shutdown_id")
        with self._state() as state:
            if shutdown_id in state["shutdowns"]:
                return False
            state["shutdowns"].append(shutdown_id)
            if not self.config.get("enabled", True):
                return False
            self._schedule(state)
            if not state["dirty"] and not any(
                j["status"] == "pending"
                and j["scope"] == "instance"
                and datetime.fromisoformat(j["due"]) <= self._now()
                for j in state["jobs"].values()
            ):
                return False
            for pending in state["jobs"].values():
                if (
                    pending["status"] == "pending"
                    and pending["scope"] == "instance"
                    and not pending["manual"]
                    and pending["attempt"] == 0
                    and datetime.fromisoformat(pending["due"]) <= self._now()
                ):
                    pending["trigger"] = "shutdown"
                    return True
            return self._enqueue(
                state, "shutdown:" + shutdown_id, "shutdown", "instance", merge=False
            )
