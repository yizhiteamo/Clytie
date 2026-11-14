"""Accepted memory operations outlive their HTTP waiters."""

import asyncio
import json
import subprocess
import sys
from uuid import uuid4

from clytie.backup.io_budget import IoBudgetError
from clytie.backup.io_control import IoControl
from clytie.plugin_host.locking import process_identity
from clytie.plugin_host.snapshots import contained, identifier
from clytie.plugin_host.support import finish_cleanup, timed
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
        self._admissions = set()
        self.io_control = IoControl(clock, home=home)

    @property
    def still_finishing(self):
        return (
            self.cancel_since is not None
            and (bool(self.tasks) or any(not item.done() for item in self._admissions))
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

    async def execute(self, cid, op, args, operation_id=None, *, runner=None, io_session=None):
        admitted = asyncio.get_running_loop().create_future()
        self._admissions.add(admitted)
        try:
            return await self._execute(
                cid, op, args, operation_id, runner=runner, io_session=io_session, admitted=admitted
            )
        finally:
            if not admitted.done():
                admitted.set_result(None)
            self._admissions.discard(admitted)

    async def _execute(self, cid, op, args, operation_id, *, runner, io_session, admitted):
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
        try:
            await self.io_control.ensure_ready()
        except IoBudgetError as exc:
            raise APIError(exc.code, 409) from None
        if runner is None:
            await self._recover_before_admission(cid)
        if self.stopping:
            raise APIError("supervisor_stopping", 409)
        operation_id = operation_id or uuid4().hex
        own_session = io_session is None
        io_session = io_session or self.io_control.register()
        accepted = False
        reserved = False
        try:
            with gate(self.home, cid):
                if pending(self.home, cid, self.clock):
                    raise ManagementBusy()
                await self.io_control.reserve(io_session, operation_id)
                reserved = True
                if self.stopping:
                    raise APIError("supervisor_stopping", 409)
                journal = Journal.create(self.home, cid, op, self.clock, operation_id)
                journal.io_session = io_session
                journal.io_own_session = own_session
                key = journal.value["id"]
                self.journals[key] = journal
                task = asyncio.create_task(self._run(journal, args, runner or self.runner))
                self.tasks[key] = task
                task.add_done_callback(
                    lambda done: done.exception() if not done.cancelled() else None
                )
                accepted = True
        except ManagementBusy:
            raise APIError("management_busy", 409) from None
        except IoBudgetError as exc:
            raise APIError(exc.code, 409) from None
        finally:
            if not accepted:

                async def release_unstarted():
                    if reserved:
                        await self.io_control.retire_operation(operation_id)
                    if own_session:
                        await self.io_control.ended(io_session, confirmed=True)

                await finish_cleanup(release_unstarted())
        admitted.set_result(None)
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

        local_runner = runner != self._process and getattr(journal, "io_own_session", False)
        try:
            if local_runner:
                await self.io_control.bind_execution(
                    journal.io_session, key, await asyncio.to_thread(process_identity)
                )
            response = await runner(journal, args, control)
            if not response.get("ok"):
                raise APIError(
                    response.get("code", "management_worker_failed"), 409, {"operation_id": key}
                )
            result = response.get("result", Journal.load(journal.path, self.clock).public())
            if isinstance(result, dict):
                result = {**result, "operation_id": key}
            return result
        except IoBudgetError as exc:
            raise APIError(exc.code, 409, {"operation_id": key}) from None
        finally:
            try:
                if local_runner:

                    async def release_local():
                        await self.io_control.retire_operation(key)
                        await self.io_control.ended(journal.io_session, confirmed=True)

                    await finish_cleanup(release_local())
            finally:
                self.controls.pop(key, None)
                self.tasks.pop(key, None)

    async def _process(self, journal, args, control, *, recover_only=False):
        # Prepare the trusted allow-list before launching a child. A damaged or
        # blocked journal must not leave a process waiting for its initial input.
        authorized = (
            [
                item.value["id"]
                for item in pending(self.home, journal.value["companion_id"], self.clock)
            ]
            if recover_only
            else [journal.value["id"]]
        )
        executable, env = python_process()
        env.pop("CLYTIE_HOME", None)
        kwargs = (
            {"creationflags": subprocess.DETACHED_PROCESS}
            if sys.platform == "win32"
            else {"start_new_session": True}
        )
        session = getattr(journal, "io_session", None) or self.io_control.register()
        process = None
        output_drained = False
        delivery_started = False
        registered = [] if recover_only else list(authorized)

        async def send(reply):
            if process.stdin.is_closing():
                raise RuntimeError("management_control_disconnected")
            process.stdin.write(json.dumps(reply).encode("utf-8") + b"\n")
            await process.stdin.drain()

        def cancel():
            if process.returncode is None and not process.stdin.is_closing():
                try:
                    process.stdin.write(b"cancel\n")
                except (OSError, RuntimeError):
                    pass

        try:
            if recover_only:
                await self.io_control.ensure_ready()
            try:
                process, cancelled = await finish_cleanup(
                    asyncio.create_subprocess_exec(
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
                )
                if cancelled:
                    raise asyncio.CancelledError
            except OSError:
                # Nothing was delivered and no child was created.
                if not recover_only:
                    await asyncio.to_thread(journal.phase, "failed")
                return {"ok": False, "code": "management_worker_failed"}
            identity = await asyncio.to_thread(process_identity, process.pid)
            for operation_id in authorized:
                if recover_only:
                    await self.io_control.authorize_execution(
                        session, operation_id, identity, role="recovery"
                    )
                    registered.append(operation_id)
                else:
                    await self.io_control.bind_execution(session, operation_id, identity)

            def save_executor():
                for operation_id in authorized:
                    path = journal.path.with_name(operation_id + ".json")
                    fresh = Journal.load(path, self.clock)
                    fresh.value["recovery_executor" if recover_only else "executor"] = identity
                    fresh.save()

            _, cancelled = await finish_cleanup(asyncio.to_thread(save_executor))
            if cancelled:
                raise asyncio.CancelledError
            control(cancel)
            try:
                delivery_started = True
                process.stdin.write(json.dumps(args, ensure_ascii=False).encode("utf-8") + b"\n")
                await process.stdin.drain()
            except (OSError, RuntimeError):
                process.stdin.close()
            response, result_count = None, 0
            try:
                while line := await process.stdout.readline():
                    try:
                        frame = json.loads(line)
                    except (ValueError, TypeError):
                        result_count += 1
                        continue
                    if isinstance(frame, dict) and frame.get("type") == "backup_io":
                        self.io_control.dispatch(session, frame, send)
                    else:
                        result_count += 1
                        response = frame if isinstance(frame, dict) else None
            except ValueError:
                # A large result must not orphan a still-running writer or leave it
                # blocked on a full pipe. Discard transport bytes without logging them,
                # then retain the durable result rather than replaying the operation.
                self.io_control.disconnect(session)
                process.stdin.close()
                while await process.stdout.read(64 * 1024):
                    pass
                response = None
            output_drained = True
            self.io_control.disconnect(session)
            await process.wait()
            if response is None or result_count != 1:
                # Do not open SQLite or retry recovery in the daemon. A future admission
                # starts an isolated verification worker, never the original operation.
                recovered = Journal.load(journal.path, self.clock)
                if recovered.value["state"] == "accepted" and not recover_only:
                    with gate(self.home, journal.value["companion_id"]):
                        recovered.phase("failed")
                return {
                    "ok": recovered.value["committed"],
                    "code": "management_result_unconfirmed",
                    "result": recovered.public(),
                }
            return response
        finally:
            # EOF, cancellation of this waiter and transport loss are not executor
            # exit. Keep the slot and accepted task until the actual child settles.
            async def finish():
                self.io_control.disconnect(session)
                if process is not None:
                    process.stdin.close()
                    try:
                        if not output_drained:
                            while await process.stdout.read(64 * 1024):
                                pass
                    finally:
                        await process.wait()
                    try:
                        await process.stdin.wait_closed()
                    except (OSError, RuntimeError):
                        pass
                await self.io_control.ended(session, confirmed=True)
                for operation_id in registered:
                    await self.io_control.retire_operation(operation_id)
                if not delivery_started and not recover_only:
                    await asyncio.to_thread(journal.phase, "failed")

            _, cancelled = await finish_cleanup(finish())
            if cancelled:
                raise asyncio.CancelledError

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
        await asyncio.gather(*tuple(self._admissions), return_exceptions=True)
        await asyncio.gather(*tuple(self.tasks.values()), return_exceptions=True)
