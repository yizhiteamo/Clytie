"""One fictional candidate fixture using the real backup child and live owner.

No CLI, loop of scales, uploader or automatic retry. A frozen mechanism/matrix
launcher must supply the approved run identity before this can be used as evidence.
"""

import asyncio
import json
import platform
import sqlite3
from contextlib import ExitStack
from pathlib import Path
from time import perf_counter  # noqa: TID251 -- offline measurement driver
from types import SimpleNamespace
from unittest.mock import patch

try:
    from . import benchmark_backup as benchmark
    from . import measurement_evidence as evidence
    from .measurement_load import requests
    from .measurement_observation import Observation, request_scope
    from .measurement_recording import Recorder
    from .measurement_resources import file_bytes, rss_bytes
    from .measurement_views import Views
except ImportError:
    import benchmark_backup as benchmark
    import measurement_evidence as evidence
    from measurement_load import requests
    from measurement_observation import Observation, request_scope
    from measurement_recording import Recorder
    from measurement_resources import file_bytes, rss_bytes
    from measurement_views import Views

from clytie.backup.controller import CaptureBridge, capture_request
from clytie.backup.io_budget import RATE_BUSY
from clytie.backup.io_channel import IoChannel
from clytie.backup.io_control import IoControl
from clytie.backup.workers import BackupWorkers
from clytie.plugin_host.locking import process_identity
from clytie.sdk import CancelToken
from clytie.sdk.testing import FakeContext, FakeModel
from clytie.supervisor.common import Audit


async def wall_until(deadline):
    loop = asyncio.get_running_loop()
    while (remaining := deadline - perf_counter()) > 0:
        ready = loop.create_future()
        handle = loop.call_later(max(0.001, remaining), ready.set_result, None)
        try:
            await ready
        finally:
            handle.cancel()


async def settle(operation, errors, *, cancelled_ok=False):
    """Drain real cleanup even if its waiter is cancelled; retain every failure."""
    task = asyncio.ensure_future(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if not task.cancelled():
                errors.append(exc)
        except BaseException:
            break
    if task.cancelled():
        if not cancelled_ok:
            errors.append(asyncio.CancelledError())
    else:
        error = task.exception()
        if error is not None:
            errors.append(error)


async def run_fixture(directory, run, *, scale):
    """Tiny integration fixture only. Not an authorized mechanism or matrix run."""
    evidence.validate_run(run)
    if run["phase"] != "fixture" or run["method"] != "encryption_fixture_v1":
        raise ValueError("measurement_launcher_not_frozen")
    if (
        run["arm"] != "candidate"
        or run["platform"] != platform.system()
        or run["python"] != platform.python_version()
        or run["sqlite"] != sqlite3.sqlite_version
    ):
        raise ValueError("measurement_fixture_identity_mismatch")
    if scale[0] > 128 or scale[1] > 8 or scale[2] > 1024:
        raise ValueError("measurement_fixture_too_large")
    root = Path(directory).absolute()
    root.mkdir(exist_ok=False)
    (root / "measurement-fixture.json").write_text(
        json.dumps({"format": 1, "source": "generated_fictional_only"}), encoding="utf-8"
    )
    recorder = Recorder()
    origin = perf_counter()
    observation = Observation(root, recorder, origin=origin)
    views = Views(recorder, now=observation.now)
    owner = None
    workers = None
    ticking = True
    sampling = None
    last_tick = perf_counter()
    loop_delay = 0.0
    next_tick = None
    parent = process_identity()
    child_identities = {}
    child_processes = []
    operations = []
    completed = False
    errors = []
    control = session = None
    loop = asyncio.get_running_loop()

    def tick():
        nonlocal last_tick, loop_delay, next_tick
        now = perf_counter()
        loop_delay = max(0, now - last_tick - 0.005) * 1000
        owner.clock.advance(max(0, now - last_tick))
        last_tick = now
        if ticking:
            next_tick = loop.call_later(0.005, tick)

    with observation.install(), views.install(), ExitStack() as instruments:
        try:
            owner, _ = await benchmark.setup(root, scale, record_payload=True, seed=run["seed"])
            instruments.enter_context(observation.observe_owner(owner.storage_owner))
            last_tick = perf_counter()
            tick()
            control = IoControl(owner.clock)
            session = control.register(storage_owner=True)

            async def reply(value):
                channel.receive(value)

            def emit(value):
                loop.call_soon_threadsafe(lambda: control.dispatch(session, value, reply))

            channel = IoChannel(emit)
            owner.backup_io_client = channel.client

            async def request(cid, operation, arguments, **_):
                if cid != owner.cid or operation != "backup_capture":
                    raise ValueError("measurement_unexpected_operation")
                return await capture_request(owner, arguments)

            supervisor = SimpleNamespace(
                children={owner.cid: owner},
                rows={owner.cid: {"state": "running"}},
                backup_io=control,
                audit=Audit(owner.home, owner.clock),
                request=request,
                authorize_backup_capture=lambda cid, op: control.authorize(session, op),
            )
            workers = BackupWorkers(
                owner.home, owner.clock, capture_handler=CaptureBridge(supervisor)
            )
            native_spawn = asyncio.create_subprocess_exec
            bootstrap = Path(__file__).with_name("measurement_worker.py")

            async def spawn(*args, **kwargs):
                if tuple(args[1:3]) != ("-m", "clytie.backup.worker") or len(args) != 5:
                    raise ValueError("measurement_unexpected_child")
                if Path(args[3]).resolve() != owner.home.resolve():
                    raise ValueError("measurement_child_outside_fixture")
                process = await native_spawn(
                    args[0], str(bootstrap), str(root), args[4], str(origin), **kwargs
                )
                child_identities[process.pid] = process_identity(process.pid)
                child_processes.append(process)
                recorder.append(
                    "release-events.jsonl",
                    dict(id=1, at_s=observation.now(), resource="process", event="acquired"),
                )
                return process

            instruments.enter_context(patch.object(asyncio, "create_subprocess_exec", spawn))
            operation_id = "f" * 32
            model = FakeModel(FakeContext(clock=owner.clock))
            start = perf_counter()

            async def business(index):
                def command(resources, _):
                    store = resources.memory
                    store.db.execute(
                        "SELECT data FROM records WHERE id=?", (f"{index % scale[0] + 1:032x}",)
                    ).fetchone()
                    with store.coordination.commit(), store.transaction():
                        store.set_meta("benchmark_counter", index)

                with request_scope(index + 1):
                    await owner.storage_owner.execute(command)
                await model.generate(None, CancelToken())

            def resource_sample():
                total = rss_bytes(parent)
                for process in tuple(workers.processes.values()):
                    if process.returncode is not None:
                        continue
                    try:
                        total += rss_bytes(child_identities[process.pid])
                    except (ProcessLookupError, FileNotFoundError):
                        pass  # OS confirms exit; never sample a replacement PID.
                    except OSError as exc:
                        if getattr(exc, "winerror", None) != 87:
                            raise
                wal = file_bytes(owner.home, suffix="-wal")
                return total, wal, file_bytes(owner.home / "run")

            async def sample():
                counter = 0
                while True:
                    scheduled = start + counter * 0.1
                    await wall_until(scheduled)
                    entered = perf_counter()
                    pressure = owner.storage_owner.sample_io_pressure()
                    control.pressure(session, pressure)
                    status = owner.storage_owner.status()
                    budget = control.budget.status()
                    recorder.append(
                        "requests.jsonl",
                        dict(
                            id=(1 << 30) + counter,
                            kind="control",
                            scheduled_s=scheduled - origin,
                            started_s=entered - origin,
                            finished_s=perf_counter() - origin,
                            outcome="ok",
                        ),
                    )
                    rss, wal, temporary = await benchmark.measurement_thread(resource_sample)
                    recorder.append(
                        "resources.jsonl",
                        dict(
                            at_s=observation.now(),
                            rss_bytes=rss,
                            wal_bytes=wal,
                            temp_bytes=temporary,
                            queue=status["queued"],
                            finalizers=status["finalizers"],
                            views=views.pending,
                            backup_bytes=sum(budget["charged"].values()),
                            loop_delay_ms=loop_delay,
                            rate="busy" if budget["rate"] == RATE_BUSY else "normal",
                        ),
                    )
                    counter += 1

            sampling = asyncio.create_task(sample())

            async def backup():
                await wall_until(start + 0.05)
                entered = perf_counter()
                outcome = "failed"
                try:
                    await workers.execute("create", {"scope": "instance"}, operation_id)
                    # The observer is only a waiter. Cancelling it must not
                    # cancel BackupWorkers' independently tracked executors.
                    await asyncio.shield(asyncio.gather(*tuple(workers.tasks.values())))
                    result = workers.query(operation_id)
                    if (
                        result["phase"] == "committed"
                        and result["result"]["creation"] == "complete"
                    ):
                        outcome = "ok"
                    return outcome == "ok"
                except asyncio.CancelledError:
                    outcome = "cancelled"
                    raise
                finally:
                    recorder.append(
                        "requests.jsonl",
                        dict(
                            id=1 << 31,
                            kind="backup",
                            scheduled_s=start + 0.05 - origin,
                            started_s=entered - origin,
                            finished_s=perf_counter() - origin,
                            outcome=outcome,
                        ),
                    )

            operations = [
                asyncio.create_task(
                    requests(
                        recorder,
                        count=run["load"]["request_count"],
                        interval=run["load"]["interval_seconds"],
                        start=start,
                        origin=origin,
                        now=perf_counter,
                        wait=wall_until,
                        operation=business,
                    )
                ),
                asyncio.create_task(backup()),
            ]
            result = await asyncio.gather(*operations)
            completed = all(result)
            views.verify_released()
            if control.budget.status()["active"] or control.budget.status()["waiting"]:
                raise ValueError("measurement_budget_not_released")
        except BaseException as exc:
            errors.append(exc)
        finally:
            for task in operations:
                if not task.done():
                    task.cancel()
            await settle(asyncio.gather(*operations, return_exceptions=True), errors)
            if workers is not None:
                await settle(workers.stop("cancel" if not completed else "wait"), errors)
            if sampling is not None:
                sampling.cancel()
                await settle(sampling, errors, cancelled_ok=True)
            if owner is not None:
                await settle(owner.host.close(), errors)
                await settle(owner.storage_owner.close(), errors)
                try:
                    owner.runtime.close()
                except BaseException as exc:
                    errors.append(exc)
                if control is not None and not owner.storage_owner._thread.is_alive():
                    await settle(control.ended(session, confirmed=True), errors)
            ticking = False
            if next_tick is not None:
                next_tick.cancel()
            try:
                views.verify_released()
                for index, process in enumerate(child_processes, 1):
                    if process.returncode is None:
                        raise ValueError("measurement_process_exit_unconfirmed")
                    recorder.append(
                        "release-events.jsonl",
                        dict(
                            id=index,
                            at_s=observation.now(),
                            resource="process",
                            event="exit_verified",
                        ),
                    )
            except BaseException as exc:
                errors.append(exc)
    child = root / "child-storage-phases.jsonl"
    outcome = "pass" if completed and not errors else "fail"
    if any(isinstance(error, asyncio.CancelledError) for error in errors):
        outcome = "cancelled"
    manifest = {**run, "result": outcome}
    try:
        recorder.append_bytes("storage-phases.jsonl", child.read_bytes())
        recorder.finish(root / "raw", manifest)
    except BaseException as exc:
        errors.append(exc)
    if errors:
        # No exception content enters the raw schema. The caller retains its
        # local diagnostic; failed/incomplete sampling cannot become a pass.
        if len(errors) > 1:
            raise errors[0] from errors[-1]
        raise errors[0]
    return manifest
