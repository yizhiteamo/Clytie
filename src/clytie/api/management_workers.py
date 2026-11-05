"""Accepted memory operations outlive their HTTP waiters."""

import asyncio
import json
import subprocess
import sys

from clytie.plugin_host.snapshots import contained, identifier
from clytie.plugin_host.support import timed
from clytie.supervisor.processes import python_process

from .auth import APIError
from .management_journal import Journal, ManagementBusy, executor_alive, gate, pending


class Workers:
    def __init__(self, home, clock, *, runner=None, timeout=120):
        self.home, self.clock, self.timeout = home, clock, timeout
        self.runner = runner or self._process
        self.tasks, self.journals, self.controls = {}, {}, {}
        self.stopping = False
        self.cancel_since = None
        self._closing = None

    @property
    def still_finishing(self):
        return (
            self.cancel_since is not None
            and bool(self.tasks)
            and (self.clock.monotonic() - self.cancel_since) >= self.timeout
        )

    def status(self):
        paths = {j.path for j in self.journals.values()}
        companions = self.home / "companions"
        if companions.exists():
            for companion in companions.iterdir():
                directory = contained(self.home, companion / "management/operations")
                if directory.is_dir():
                    paths.update(p for p in directory.iterdir() if p.suffix == ".json")
        result = []
        for path in sorted(paths):
            try:
                result.append(Journal.load(contained(self.home, path), self.clock).public())
            except (OSError, ValueError, KeyError):
                result.append({"state": "recovery_blocked", "reason": "journal_unreadable"})
        return result

    async def execute(self, cid, op, args, operation_id=None, *, runner=None):
        if self.stopping:
            raise APIError("supervisor_stopping", 409)
        identifier(cid)
        if operation_id:
            if (
                not isinstance(operation_id, str)
                or len(operation_id) != 32
                or any(c not in "0123456789abcdef" for c in operation_id)
            ):
                raise APIError("invalid_request")
            path = contained(
                self.home,
                self.home / "companions" / cid / "management/operations" / (operation_id + ".json"),
            )
            if path.exists():
                self.journals[operation_id] = Journal.load(path, self.clock)
        if operation_id and operation_id in self.journals:
            existing = self.journals[operation_id]
            if existing.value["companion_id"] != cid or existing.value["operation"] != op:
                raise APIError("invalid_request")
            # A repeated id is a status lookup, never a second execution or secret delivery.
            return Journal.load(existing.path, self.clock).public()
        if runner is None:
            await self._recover_before_admission(cid)
        if self.stopping:
            raise APIError("supervisor_stopping", 409)
        try:
            with gate(self.home, cid):
                if pending(self.home, cid, self.clock):
                    raise ManagementBusy()
                journal = Journal.create(self.home, cid, op, self.clock, operation_id)
                key = journal.value["id"]
                self.journals[key] = journal
                task = asyncio.create_task(self._run(journal, args, runner or self.runner))
                self.tasks[key] = task
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
        except ManagementBusy:
            raise APIError("management_busy", 409) from None
        try:
            return await timed(
                asyncio.shield(task),
                self.clock,
                600 if op == "export" or op.startswith("import/") else 30,
            )
        except TimeoutError:
            raise APIError("companion_timeout", 504, {"operation_id": key}) from None

    async def _recover_before_admission(self, cid):
        try:
            with gate(self.home, cid):
                unresolved = pending(self.home, cid, self.clock)
                if not unresolved:
                    return
                if any(
                    journal.value["id"] in self.tasks
                    or executor_alive(journal.value.get("executor")) is True
                    for journal in unresolved
                ):
                    raise ManagementBusy()
                journal = unresolved[0]
                key = journal.value["id"]
                self.journals[key] = journal

                async def recover(journal, args, control):
                    return await self._process(journal, args, control, recover_only=True)

                task = asyncio.create_task(self._run(journal, {}, recover))
                self.tasks[key] = task
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
            await asyncio.shield(task)
        except ManagementBusy:
            raise APIError("management_busy", 409) from None

    async def _run(self, journal, args, runner):
        key = journal.value["id"]

        def control(callback):
            self.controls[key] = callback
            if self.cancel_since is not None:
                callback()

        try:
            response = await runner(journal, args, control)
            if not response.get("ok"):
                raise APIError(
                    response.get("code", "management_worker_failed"), 409, {"operation_id": key}
                )
            result = response.get("result", Journal.load(journal.path, self.clock).public())
            if isinstance(result, dict):
                result = {**result, "operation_id": key}
            return result
        finally:
            self.controls.pop(key, None)
            self.tasks.pop(key, None)

    async def _process(self, journal, args, control, *, recover_only=False):
        executable, env = python_process()
        env.pop("CLYTIE_HOME", None)
        kwargs = (
            {"creationflags": subprocess.DETACHED_PROCESS}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        try:
            process = await asyncio.create_subprocess_exec(
                executable,
                "-m",
                "clytie.api.management_worker",
                str(self.home),
                journal.value["companion_id"],
                journal.value["id"],
                *(["recover"] if recover_only else []),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                limit=8 * 1024 * 1024,
                **kwargs,
            )
        except OSError:
            # No child was created, so no business operation or recovery may run.
            with gate(self.home, journal.value["companion_id"]):
                if not recover_only:
                    journal.phase("failed")
            return {"ok": False, "code": "management_worker_failed"}
        process.stdin.write(json.dumps(args, ensure_ascii=False).encode("utf-8") + b"\n")
        await process.stdin.drain()

        def cancel():
            if process.returncode is None and not process.stdin.is_closing():
                try:
                    process.stdin.write(b"cancel\n")
                except (OSError, RuntimeError):
                    pass

        control(cancel)
        try:
            line = await process.stdout.readline()
            await process.wait()
            if not line:
                # Do not open SQLite or retry recovery in the daemon. A future admission
                # starts an isolated verification worker, never the original operation.
                recovered = Journal.load(journal.path, self.clock)
                return {
                    "ok": recovered.value["committed"],
                    "code": "management_result_unconfirmed",
                    "result": recovered.public(),
                }
            return json.loads(line)
        finally:
            process.stdin.close()

    def escalate(self):
        if not self.stopping:
            return
        if self.cancel_since is None:
            self.cancel_since = self.clock.monotonic()
            for key in tuple(self.tasks):
                self.journals[key].request_cancel()
            for callback in tuple(self.controls.values()):
                callback()

    async def stop(self, mode):
        self.stopping = True
        if mode == "cancel":
            self.escalate()
        if self._closing is None:
            self._closing = asyncio.create_task(self._drain())
        await asyncio.shield(self._closing)

    async def _drain(self):
        await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
