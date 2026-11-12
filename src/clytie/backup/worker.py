"""One isolated backup operation, with durable delivery and cooperative controls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
from contextlib import redirect_stdout
from pathlib import Path
from uuid import uuid4

from clytie.clock import SystemClock
from clytie.plugin_host.locking import OperationBusy, file_lock, process_identity
from clytie.plugin_host.snapshots import canonical, contained

from .io_channel import IoChannel
from .io_client import io_scope
from .journal import TERMINAL, BackupJournal
from .restore import RestoreManager, digest, read_state, validate_contents


class WorkerJournal(BackupJournal):
    """Bounded OS-lock contention wait only in the isolated worker, never the API loop."""

    def _change(self, change):
        for attempt in range(501):
            try:
                return super()._change(change)
            except OperationBusy:
                if attempt == 500:
                    raise
                threading.Event().wait(0.01)


class Cancelled(Exception):
    pass


class Controls:
    def __init__(self, journal, stream, *, verification_only=False, emit=None):
        self.journal = journal
        self.verification_only = verification_only
        self.started = threading.Event()
        self.cancelled = threading.Event()
        self.received = False
        self.emit = emit
        self.responses = {}
        self.response_ready = threading.Condition()
        self.io_channel = IoChannel(emit)
        if stream is not None:
            threading.Thread(target=self._read, args=(stream,), daemon=True).start()

    def _read(self, stream):
        try:
            for line in stream:
                command = line.strip()
                if command == b"start":
                    self.received = True
                    self.started.set()
                elif command == b"cancel":
                    self.cancelled.set()
                    self.started.set()
                elif len(command) <= 64 * 1024 and command.startswith(b"{"):
                    try:
                        value = json.loads(command)
                        if self.io_channel.receive(value):
                            continue
                        if value.get("type") == "bridge_reply":
                            with self.response_ready:
                                self.responses[value["id"]] = value
                                self.response_ready.notify_all()
                    except (ValueError, KeyError, TypeError):
                        pass
        finally:
            self.cancelled.set()
            self.started.set()
            self.io_channel.disconnect()

    def request(self, operation, arguments):
        key = uuid4().hex
        self.emit(dict(type="bridge", id=key, operation=operation, arguments=arguments))
        with self.response_ready:
            while key not in self.responses:
                self.check()
                self.response_ready.wait(0.05)
            response = self.responses.pop(key)
        if "error" in response:
            raise ValueError("snapshot_unavailable")
        return response["result"]

    def configure_capture(self, home):
        context = self.request("context", {})

        async def online(cid, work, epoch):
            reply = await asyncio.to_thread(
                self.request, "capture", dict(companion_id=cid, work=str(work), epoch=epoch)
            )
            path = contained(home, Path(reply["path"]))
            if digest(path) != reply["sha256"]:
                raise ValueError("capture_evidence_mismatch")
            return json.loads(path.read_bytes())

        controls = self

        class Audit:
            def seal(self):
                reply = controls.request("audit", {})
                path = contained(home, Path(reply["path"]))
                if digest(path) != reply["sha256"]:
                    raise ValueError("capture_evidence_mismatch")
                return json.loads(path.read_bytes())

        self.capture_context = dict(
            online_ids=frozenset(context["online_ids"]), online=online, audit_writers=(Audit(),)
        )

    def check(self):
        if self.verification_only or self.journal.value["phase"] in {"publishing", *TERMINAL}:
            return
        self.check_optional()

    def check_optional(self):
        """副本等可选收尾可以取消，但不改变主包已经发布的事实。"""
        fresh = WorkerJournal.load(self.journal.path, self.journal.clock)
        if self.cancelled.is_set() or fresh.value["cancel_requested"]:
            if not fresh.value["cancel_requested"]:
                fresh.request_cancel()
            raise Cancelled()


class JournalRestore(RestoreManager):
    def __init__(self, home, clock, journal, controls):
        super().__init__(home, clock)
        self.journal, self.controls = journal, controls
        self.workspace = None

    def _publish_plan(self, plan):
        self.controls.check()
        artifact = dict(
            kind="restore_plan",
            path=str(self._path(plan["id"])),
            sha256=hashlib.sha256(canonical(plan)).hexdigest(),
            workspace=str(self.workspace),
        )
        self.journal.phase(
            "publishing",
            artifacts=[artifact],
            envelope=plan["envelope"],
            source_sha256=plan["source_sha256"],
        )
        super()._publish_plan(plan)

    def _finish(self, plan, state):
        self.journal.phase("verifying")
        self.controls.check()
        self.journal.phase("publishing")
        return super()._finish(plan, state)


def _restore_result(state):
    ready = state["data_state"] == "ready_for_switch"
    return dict(
        restore_id=state["restore_id"],
        verification="passed" if ready else "failed",
        restore_readiness=state["data_state"],
    )


def dispatch(home, clock, journal, controls):
    """Fixed internal dispatch; never import a client-supplied module or script."""
    operation, args = journal.value["operation"], journal.value["arguments"]
    manager = JournalRestore(home, clock, journal, controls)
    controls.check()
    if operation in {"cleanup_plan", "cleanup_apply"}:
        from .cleanup import CleanupService

        service = CleanupService(home, clock)
        plan_id = journal.value["id"] if operation == "cleanup_plan" else args["plan_id"]
        artifact = dict(kind="cleanup_plan", plan_id=plan_id, path=str(service._path(plan_id)))
        if operation == "cleanup_apply":
            artifact["sha256"] = service.fingerprint(plan_id)
        journal.phase("publishing", artifacts=[artifact])
        if operation == "cleanup_plan":
            plan = service.plan(
                args["category"],
                old_id=args.get("old_id"),
                preserved=args.get("preserved"),
                backup_ids=args.get("backup_ids"),
                plan_id=plan_id,
            )
            return dict(plan_id=plan["id"], verification="passed")
        from .old_instances import OldInstanceError

        def cleanup_checkpoint():
            try:
                getattr(controls, "check_optional", controls.check)()
            except Cancelled:
                # The core must persist stopped/progress and release only its
                # own retirement before the worker records cancellation.
                raise OldInstanceError("cancelled") from None

        plan = service.apply(plan_id, check=cleanup_checkpoint)
        if plan["state"] == "stopped" and plan.get("reason") == "cancelled":
            raise Cancelled()
        return dict(
            plan_id=plan["id"], verification="passed" if plan["state"] == "completed" else "failed"
        )
    if operation == "restore_plan":
        work = contained(home, home / "run/backup-work" / journal.value["id"] / "plan")
        work.mkdir(parents=True, exist_ok=False)
        manager.workspace = work
        journal.phase(
            "copying",
            artifacts=[
                dict(kind="workspace", path=str(work)),
                dict(kind="restore_plan_expected", path=str(manager._path(journal.value["id"]))),
            ],
        )
        plan = manager.plan(
            Path(args["source"]),
            Path(args["key_path"]),
            Path(args["target"]),
            work,
            check=controls.check,
            plan_id=journal.value["id"],
            source_repository=args.get("source_repository"),
            confirmed=args.get("source_repository_confirmed", False),
        )
        return dict(plan_id=plan["id"], verification="passed", restore_readiness="planned")
    if operation in {"restore_apply", "recover"}:
        plan = manager._load(args["plan_id"])
        artifacts = [
            dict(
                kind="restore_plan",
                path=str(manager._path(plan["id"])),
                sha256=digest(manager._path(plan["id"])),
            ),
            dict(
                kind="restore_target",
                path=plan["target"],
                plan_id=plan["id"],
                source_sha256=plan["source_sha256"],
            ),
        ]
        journal.phase(
            "copying" if operation == "restore_apply" else "verifying",
            artifacts=artifacts,
            source_sha256=plan["source_sha256"],
        )
        state = (
            manager.apply(plan["id"], check=controls.check, operation_id=journal.value["id"])
            if operation == "restore_apply"
            else manager.recover(plan["id"])
        )
        return _restore_result(state)
    if operation in {"create", "verify"}:
        from .creation import dispatch as creation_dispatch

        return creation_dispatch(home, clock, journal, controls)
    raise ValueError("unsupported_operation")


def verify_previous(home, clock, journal, controls):
    """Only registered artifacts may be checked; never rerun capture/decryption."""
    operation = journal.value["operation"]
    artifacts = journal.value.get("artifacts", [])
    if operation in {"cleanup_plan", "cleanup_apply"}:
        from .cleanup import CleanupService

        candidates = [a for a in artifacts if a["kind"] == "cleanup_plan"]
        if len(candidates) != 1:
            journal.phase("failed", reason="cleanup_not_started")
            return
        artifact = candidates[0]
        service = CleanupService(home, clock)
        expected_id = (
            journal.value["id"]
            if operation == "cleanup_plan"
            else journal.value["arguments"]["plan_id"]
        )
        if artifact["plan_id"] != expected_id or Path(artifact["path"]) != service._path(
            expected_id
        ):
            raise ValueError("plan_evidence_mismatch")
        if not service._path(expected_id).is_file():
            journal.phase("failed", reason="plan_not_published")
            return
        if operation == "cleanup_apply" and service.fingerprint(expected_id) != artifact["sha256"]:
            raise ValueError("plan_evidence_mismatch")
        plan = service.operation(expected_id)
        if operation == "cleanup_plan":
            args = journal.value["arguments"]
            if (
                plan["category"] != args["category"]
                or plan.get("old_id") != args.get("old_id")
                or plan.get("backup_ids") != args.get("backup_ids")
            ):
                raise ValueError("plan_evidence_mismatch")
            journal.commit(dict(plan_id=expected_id, verification="passed"))
        else:
            plan = service.recover(expected_id)
            journal.commit(
                dict(
                    plan_id=expected_id,
                    verification="passed" if plan["state"] == "completed" else "failed",
                )
            )
        return
    plans = [a for a in artifacts if a["kind"] == "restore_plan"]
    if operation == "restore_plan":
        if len(plans) != 1 or not Path(plans[0]["path"]).is_file():
            journal.phase("failed", reason="plan_not_published")
            return
        artifact = plans[0]
        path = contained(home, Path(artifact["path"]))
        if digest(path) != artifact["sha256"]:
            journal.phase("recovery_blocked", reason="plan_evidence_mismatch")
            return
        manager = RestoreManager(home, clock)
        plan = manager._load(journal.value["id"])
        if plan["source_sha256"] != journal.value.get("source_sha256") or plan[
            "envelope"
        ] != journal.value.get("envelope"):
            journal.phase("recovery_blocked", reason="plan_evidence_mismatch")
            return
        workspace = contained(home, Path(artifact["workspace"]))
        journal.phase("verifying")
        validate_contents(workspace, plan["envelope"], repo_root=manager.repo_root)
        manager.publish_preview(plan)
        journal.commit(dict(plan_id=plan["id"], verification="passed", restore_readiness="planned"))
        return
    if operation in {"restore_apply", "recover"}:
        targets = [a for a in artifacts if a["kind"] == "restore_target"]
        if len(plans) != 1 or len(targets) != 1:
            journal.phase("failed", reason="restore_not_started")
            return
        if digest(contained(home, Path(plans[0]["path"]))) != plans[0]["sha256"]:
            journal.phase("recovery_blocked", reason="plan_evidence_mismatch")
            return
        target = targets[0]
        manager = JournalRestore(home, clock, journal, controls)
        plan = manager._load(target["plan_id"])
        if plan["target"] != target["path"] or plan["source_sha256"] != target["source_sha256"]:
            journal.phase("recovery_blocked", reason="target_evidence_mismatch")
            return
        state = read_state(Path(target["path"]))
        if state is None:
            journal.phase("failed", reason="restore_not_started")
            return
        if (
            state["source_sha256"] != target["source_sha256"]
            or state["restore_id"] != target["plan_id"]
        ):
            journal.phase("recovery_blocked", reason="target_evidence_mismatch")
            return
        journal.phase("verifying")
        state = manager.recover(target["plan_id"])
        if state["data_state"] == "ready_for_switch":
            journal.commit(_restore_result(state))
        else:
            journal.phase("recovery_blocked", reason="restore_incomplete")
        return
    if operation in {"create", "verify"}:
        from .creation import verify_previous as creation_verify

        creation_verify(home, clock, journal, controls)
        return
    journal.phase("failed", reason="operation_not_started")


def execute(home, operation_id, *, stream, emit, clock=None, recover_only=False):
    home, clock = Path(home).resolve(), clock or SystemClock()
    path = BackupJournal.path_for(home, operation_id)
    try:
        with file_lock(
            home / "run/backup-worker.lock",
            {**process_identity(), "operation": "backup_worker", "operation_id": operation_id},
        ):
            journal = WorkerJournal.load(path, clock)
            if journal.value["phase"] in TERMINAL or (
                not recover_only and journal.value["phase"] != "accepted"
            ):
                return journal.public()
            journal.phase(
                "preparing" if not recover_only else "verifying",
                executor=process_identity(),
                delivery="waiting",
            )
            controls = Controls(journal, stream, verification_only=recover_only, emit=emit)
            emit(dict(type="ready", id=operation_id))
            controls.started.wait()
            if not controls.received:
                journal.request_cancel()
                journal.phase(
                    "cancelled" if not recover_only else "recovery_blocked",
                    reason="delivery_cancelled",
                )
                return journal.public()
            journal.phase(journal.value["phase"], delivery="received")
            emit(dict(type="started", id=operation_id))
            try:
                if recover_only:
                    # An EOF during verification must not erase already committed facts.
                    verify_previous(home, clock, journal, controls)
                else:
                    controls.check()
                    if journal.value["operation"] == "create":
                        controls.configure_capture(home)
                    client = controls.io_channel.client(operation_id, check=controls.check)
                    with io_scope(client):
                        journal.commit(dispatch(home, clock, journal, controls))
            except Cancelled:
                fresh = WorkerJournal.load(path, clock)
                if fresh.value["phase"] not in TERMINAL:
                    fresh.phase("cancelled", reason="cancelled")
            except Exception as exc:
                from .errors import error_reason

                fresh = WorkerJournal.load(path, clock)
                if fresh.value["phase"] not in TERMINAL:
                    # Publication may already have happened; the next isolated verifier
                    # uses artifacts, never the original request.
                    fresh.phase(
                        "recovery_blocked" if fresh.value["phase"] == "publishing" else "failed",
                        reason=error_reason(
                            exc, "verification_failed" if recover_only else "worker_failed"
                        ),
                    )
            return WorkerJournal.load(path, clock).public()
    except OperationBusy:
        return WorkerJournal.load(path, clock).public()


def main():
    home, operation_id = Path(sys.argv[1]).resolve(), sys.argv[2]
    recover_only = len(sys.argv) > 3 and sys.argv[3] == "recover-only"
    protocol = sys.stdout.buffer
    output_lock = threading.Lock()

    def emit(value):
        with output_lock:
            protocol.write(json.dumps(value, ensure_ascii=False).encode() + b"\n")
            protocol.flush()

    try:
        with redirect_stdout(sys.stderr):
            result = execute(
                home,
                operation_id,
                stream=sys.stdin.buffer.raw,
                emit=emit,
                recover_only=recover_only,
            )
        emit(dict(type="result", result=result))
    except Exception:
        emit(dict(type="result", code="worker_unconfirmed", id=operation_id))


if __name__ == "__main__":
    main()
