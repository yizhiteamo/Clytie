"""Observed tiny trials and registered frozen mechanism executions.

The existing run entry stays tiny-only. run_mechanism claims fixed registration
before product activation. Neither entry grants cross-runner execution permission.
No retry, matrix loop, upload or policy tuning."""

import asyncio
import hashlib
import json
import os
import platform
import sqlite3
import traceback
from contextlib import ExitStack, contextmanager
from pathlib import Path
from time import perf_counter  # noqa: TID251 -- offline measurement driver
from types import SimpleNamespace
from unittest.mock import patch

try:
    from . import measurement_evidence as evidence
    from .measurement_load import requests
    from .measurement_observation import Observation, request_scope
    from .measurement_recording import Recorder
    from .measurement_version import verify, verify_imports
except ImportError:
    import measurement_evidence as evidence
    from measurement_load import requests
    from measurement_observation import Observation, request_scope
    from measurement_recording import Recorder
    from measurement_version import verify, verify_imports

MODES = {
    "baseline": "legacy",
    "none": "candidate",
    "original": "original",
    "candidate": "candidate",
}


class TrialError(ValueError):
    def __init__(self, code, manifest=None):
        self.code, self.manifest = code, manifest
        super().__init__(code)


def register_child(processes, identities, child, identify):
    identities[child.pid] = identify(child.pid)
    processes.append(child)


async def capture_request(*args, **kwargs):
    from clytie.backup.controller import capture_request as actual

    return await actual(*args, **kwargs)


def _identity(run, product_root):
    import clytie

    product_root = Path(product_root).resolve(strict=True)
    if Path(clytie.__file__).resolve().parents[2] != product_root:
        raise ValueError("measurement_product_root_mismatch")
    inventory = verify(product_root.parent)
    if any(inventory[key] != run[key] for key in ("candidate", "source_commit")) or (
        inventory["mode"] != MODES[run["arm"]]
    ):
        raise ValueError("measurement_source_identity_mismatch")
    actual = dict(
        cpu_count=os.cpu_count(),
        os=platform.system(),
        release_sha256=hashlib.sha256(platform.release().encode()).hexdigest(),
        machine_sha256=hashlib.sha256(platform.machine().encode()).hexdigest(),
    )
    if (
        run["platform"] != platform.system()
        or run["python"] != platform.python_version()
        or run["sqlite"] != sqlite3.sqlite_version
        or run["environment"] != actual
    ):
        raise ValueError("measurement_runtime_identity_mismatch")
    verify_imports(product_root)
    return product_root


async def wall_until(deadline):
    loop = asyncio.get_running_loop()
    while (remaining := deadline - perf_counter()) > 0:
        ready = loop.create_future()
        handle = loop.call_later(max(0.001, remaining), ready.set_result, None)
        try:
            await ready
        finally:
            handle.cancel()


async def _drain(operation, errors, *, cancelled_ok=False):
    task = asyncio.ensure_future(operation)
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if not task.cancelled():
                errors.append("measurement_cleanup_wait_cancelled")
        except BaseException:
            break
    if task.cancelled():
        if not cancelled_ok:
            errors.append("measurement_cleanup_cancelled")
    elif task.exception() is not None:
        errors.append("measurement_cleanup_failed")


async def _thread(function):
    task = asyncio.create_task(asyncio.to_thread(function))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        errors = []
        await _drain(task, errors)
        raise


@contextmanager
def _memory_lifecycle(recorder, observation):
    from clytie.memory.store import MemoryStore

    original_init, original_close = MemoryStore.__init__, MemoryStore.close
    references, released = {}, set()

    def event(identifier, name):
        if not recorder.failed:
            try:
                recorder.append(
                    "release-events.jsonl",
                    dict(id=identifier, at_s=observation.now(), resource="reference", event=name),
                )
            except Exception:
                # Do not invalidate a successful constructor before its caller
                # can take responsibility for closing the real connection.
                recorder.failed = True

    def opened(store, *args, **kwargs):
        original_init(store, *args, **kwargs)
        references[store] = len(references) + 1
        event(references[store], "acquired")

    def closed(store):
        result = original_close(store)
        if store in references and (recorder.format_version not in {5, 6} or store not in released):
            event(references[store], "released")
            released.add(store)
        return result

    with patch.object(MemoryStore, "__init__", opened), patch.object(MemoryStore, "close", closed):
        yield


def _preserve_invalid(root, recorder, manifest):
    """Local diagnostic only. Empty/truncated bytes stay invalid, never synthesized."""
    target = root / "invalid-raw"
    target.mkdir(exist_ok=False)
    for name, raw in recorder.rows.items():
        with (target / name).open("xb") as stream:
            stream.write(bytes(raw))
    with (target / "run-manifest.json").open("x", encoding="utf-8") as stream:
        json.dump({**manifest, "result": "incomplete"}, stream, separators=(",", ":"))


async def run(directory, run, product_root, tiny=False):
    evidence.validate_run(run)
    if (
        tiny is not True
        or run["format"] not in {4, 5, 6}
        or run["phase"] != "fixture"
        or run["method"] != "legal_fixture_comparison_v1"
    ):
        raise ValueError("measurement_launcher_not_frozen")
    return await _execute(directory, run, product_root, tiny=True)


async def run_mechanism(directory, frozen, execution_id, version_directory):
    # No caller-supplied manifest or tiny override. Claim before activation/setup.
    try:
        from . import measurement_mechanism_launch as driver
        from .measurement_version import activate
    except ImportError:
        import measurement_mechanism_launch as driver
        from measurement_version import activate

    manifest = driver.claim_started(frozen, execution_id, version_directory)
    identity = driver.plan.digest(manifest)
    product_root, _ = activate(version_directory)
    try:
        return await _execute(directory, manifest, product_root, tiny=False)
    finally:
        verify_imports(product_root)
        after, _ = driver.checked(frozen, execution_id, version_directory)
        if driver.plan.digest(after) != identity:
            raise ValueError("measurement_material_changed")


async def _execute(directory, run, product_root, *, tiny):
    scenario = run.get("scenario", "normal")
    if scenario not in {"normal", "cancel"} or (
        scenario == "cancel"
        and (
            run["format"] != 6
            or run["arm"] != "candidate"
            or run["scale"] not in {"large_database", "large_asset"}
        )
    ):
        raise ValueError("measurement_cancel_plan_invalid")
    product_root = _identity(run, product_root)
    try:
        from . import measurement_fixture as fixture
        from .measurement_resources import file_bytes, rss_bytes
        from .measurement_views import Views
    except ImportError:
        import measurement_fixture as fixture
        from measurement_resources import file_bytes, rss_bytes
        from measurement_views import Views

    from clytie.backup.controller import CaptureBridge
    from clytie.backup.workers import BackupWorkers
    from clytie.plugin_host.locking import process_identity
    from clytie.supervisor.common import Audit

    root = Path(directory).absolute()
    if root.exists() or root.is_symlink():
        raise ValueError("measurement_destination_exists")
    root.parent.mkdir(parents=True, exist_ok=True)
    recorder, origin = Recorder(format_version=run["format"]), perf_counter()
    observation = Observation(
        root.parent,
        recorder,
        origin=origin,
        process_sequence=0 if run["format"] in {5, 6} else None,
    )
    mode, backup_enabled = MODES[run["arm"]], run["arm"] in {"original", "candidate"}
    views = (
        Views(recorder, now=observation.now, first_id=2 if run["format"] in {5, 6} else 1)
        if mode != "legacy"
        else None
    )
    owner = workers = control = session = sampling = deadline = None
    planned_cancel = None
    operations, processes, sample_processes, identities, errors = [], [], [], {}, []
    child_exit_times = {}
    diagnostics = []
    parent_identity = process_identity()
    metrics = run["metrics"]
    cancelled = completed = timed_out = False
    backup_finished = last_business_finished = None
    tick_handle, ticking = None, False
    previous_tick, loop_delay = origin, 0.0
    start = None
    loop = asyncio.get_running_loop()

    def release(identifier, resource, event):
        if not recorder.failed:
            try:
                recorder.append(
                    "release-events.jsonl",
                    dict(id=identifier, at_s=observation.now(), resource=resource, event=event),
                )
            except Exception:
                recorder.failed = True

    def tick():
        nonlocal previous_tick, loop_delay, tick_handle
        now = perf_counter()
        loop_delay = max(0, now - previous_tick - 0.005) * 1000
        owner.clock.advance(max(0, now - previous_tick))
        previous_tick = now
        if ticking:
            tick_handle = loop.call_later(0.005, tick)

    instruments = ExitStack()
    with observation.install(), _memory_lifecycle(recorder, observation):
        try:
            owner, metrics = await fixture.setup(
                root,
                run["scale"],
                mode,
                run["seed"],
                tiny=tiny,
                product_root=product_root,
            )
            if run["fixture_version"] != fixture.FIXTURE_VERSION:
                raise ValueError("measurement_fixture_version_mismatch")
            metrics = {**metrics, "backup_bytes": 0}
            if any(metrics[key] != run["metrics"][key] for key in ("logical", "ledger_rows")):
                raise ValueError("measurement_fixture_identity_mismatch")
            (root / "measurement-fixture.json").write_text(
                json.dumps(dict(format=1, source="generated_fictional_only")),
                encoding="utf-8",
            )
            if run["format"] in {5, 6}:
                (root / "measurement-observers.json").write_text(
                    json.dumps(
                        dict(
                            format=1,
                            execution_id=run["execution_id"],
                            observer_sequences=run["observer_sequences"],
                        )
                    ),
                    encoding="utf-8",
                )
            if owner.storage_owner is not None:
                instruments.enter_context(observation.observe_owner(owner.storage_owner))
                release(1, "thread", "acquired")
            if views is not None:
                instruments.enter_context(views.install())
            previous_tick, ticking = perf_counter(), True
            tick()
            if backup_enabled and mode == "candidate":
                from clytie.backup.io_channel import IoChannel
                from clytie.backup.io_control import IoControl

                control = IoControl(owner.clock, home=owner.home)
                session = control.register(storage_owner=True)
                observation.bind_pressure_source(owner.storage_owner, control, session)

                async def reply(message):
                    channel.receive(message)

                def emit(message):
                    loop.call_soon_threadsafe(lambda: control.dispatch(session, message, reply))

                channel = IoChannel(emit)
                owner.backup_io_client = channel.client
                control.pressure(session, owner.storage_owner.sample_io_pressure())

            if backup_enabled:

                async def request(cid, operation, arguments, **kwargs):
                    if cid != owner.cid or operation != "backup_capture":
                        raise ValueError("measurement_unexpected_operation")
                    before_send = kwargs.get("before_send")
                    if before_send is not None:
                        await before_send(owner)
                    return await capture_request(owner, arguments)

                supervisor = SimpleNamespace(
                    children={owner.cid: owner},
                    rows={owner.cid: {"state": "running"}},
                    audit=Audit(owner.home, owner.clock),
                    request=request,
                )
                if control is not None:

                    async def authorize(cid, operation_id, *, child=None):
                        if cid != owner.cid or child is not owner:
                            raise ValueError("measurement_unexpected_executor")
                        await control.authorize_execution(
                            session, operation_id, process_identity(), role="capture"
                        )

                    supervisor.backup_io = control
                    supervisor.authorize_backup_capture = authorize
                workers = BackupWorkers(
                    owner.home, owner.clock, capture_handler=CaptureBridge(supervisor)
                )
                if scenario == "cancel":
                    try:
                        from .measurement_cancel import PlannedCancel
                    except ImportError:
                        from measurement_cancel import PlannedCancel
                    planned_cancel = PlannedCancel(
                        observation, workers, "e" * 32, scale=run["scale"]
                    )
                    instruments.enter_context(planned_cancel.install())
                native_spawn = asyncio.create_subprocess_exec
                wrapper = Path(__file__).with_name("measurement_worker.py")

                async def spawn(*args, **kwargs):
                    if (
                        tuple(args[1:3]) != ("-m", "clytie.backup.worker")
                        or len(args) != 5
                        or Path(args[3]).resolve() != owner.home.resolve()
                    ):
                        raise ValueError("measurement_unexpected_child")
                    child_number = len(processes) + 1
                    extra = ()
                    if run["format"] in {5, 6}:
                        if child_number not in run["observer_sequences"]:
                            raise ValueError("measurement_unplanned_child")
                        extra = (str(child_number), run["execution_id"])
                    child = await native_spawn(
                        args[0],
                        str(wrapper),
                        str(root),
                        args[4],
                        str(origin),
                        str(run["format"]),
                        *extra,
                        **kwargs,
                    )
                    processes.append(child)
                    release(child_number, "process", "acquired")
                    original_wait = child.wait

                    async def observed_wait():
                        result = await original_wait()
                        if child_number not in child_exit_times:
                            child_exit_times[child_number] = perf_counter()
                            release(child_number, "process", "exit_verified")
                        return result

                    child.wait = observed_wait
                    try:
                        register_child(sample_processes, identities, child, process_identity)
                    except BaseException:
                        # Observation failure must not orphan a child which has
                        # not yet been handed back to the production tracker.
                        child.stdin.close()
                        await _drain(child.wait(), errors)
                        raise
                    return child

                instruments.enter_context(patch.object(asyncio, "create_subprocess_exec", spawn))

            for index in range(run["execution"]["warmup_requests"]):
                with request_scope(0):
                    await fixture.business(owner, index)
            start = perf_counter()

            def resources():
                total = rss_bytes(parent_identity)
                for child in tuple(sample_processes):
                    if child.returncode is not None:
                        continue
                    try:
                        total += rss_bytes(identities[child.pid])
                    except (ProcessLookupError, FileNotFoundError):
                        pass
                    except OSError as exc:
                        if getattr(exc, "winerror", None) != 87:
                            raise
                phase = "not_applicable"
                package_bytes = 0
                if backup_enabled:
                    try:
                        phase = workers.query("e" * 32)["phase"]
                    except FileNotFoundError:
                        phase = "not_started"
                    # Atomic publication may move this exact temporary package
                    # between observations. Count physical bytes, not logical
                    # budget charges, in every version's backup_bytes series.
                    pending = owner.home / "run/backup-work" / ("e" * 32) / "package.pending"
                    try:
                        package_bytes = pending.stat().st_size
                    except FileNotFoundError:
                        pass
                    final = owner.home / "backups/regular" / ("e" * 32 + ".clytie-backup")
                    try:
                        package_bytes = final.stat().st_size
                    except FileNotFoundError:
                        pass
                return (
                    total,
                    file_bytes(owner.home, suffix="-wal"),
                    file_bytes(owner.home / "run"),
                    package_bytes,
                    phase,
                )

            async def sample():
                index = 0
                while True:
                    scheduled = start + index * 0.1
                    await wall_until(scheduled)
                    entered = perf_counter()
                    status = (
                        owner.storage_owner.status() if owner.storage_owner is not None else None
                    )
                    if control is not None:
                        control.pressure(session, owner.storage_owner.sample_io_pressure())
                    recorder.append(
                        "requests.jsonl",
                        dict(
                            id=(1 << 30) + index,
                            kind="control",
                            scheduled_s=scheduled - origin,
                            started_s=entered - origin,
                            finished_s=perf_counter() - origin,
                            outcome="ok",
                        ),
                    )
                    rss, wal, temporary, package_bytes, phase = await _thread(resources)
                    budget = control.budget.status() if control is not None else None
                    recorder.append(
                        "resources.jsonl",
                        dict(
                            at_s=observation.now(),
                            rss_bytes=rss,
                            wal_bytes=wal,
                            temp_bytes=temporary,
                            queue=status["queued"] if status else 0,
                            finalizers=status["finalizers"] if status else 0,
                            views=views.pending if views else 0,
                            backup_bytes=package_bytes,
                            backup_phase=phase,
                            logical_io_bytes=sum(budget["charged"].values()) if budget else None,
                            loop_delay_ms=loop_delay,
                            rate=("busy" if budget["rate"] == 4 * 1024 * 1024 else "normal")
                            if budget
                            else "none",
                        ),
                    )
                    index += 1

            async def business(index):
                nonlocal last_business_finished
                try:
                    with request_scope(index + 1):
                        await fixture.business(owner, index)
                finally:
                    last_business_finished = perf_counter()

            async def backup():
                nonlocal backup_finished
                scheduled = start + run["load"]["backup_start_seconds"]
                await wall_until(scheduled)
                entered, outcome = perf_counter(), "failed"
                try:
                    operation = "e" * 32
                    await workers.execute("create", {"scope": "instance"}, operation)
                    await asyncio.shield(asyncio.gather(*tuple(workers.tasks.values())))
                    result = workers.query(operation)
                    if planned_cancel is not None:
                        await planned_cancel.settle()
                        result = workers.query(operation)
                        if planned_cancel.terminal(result):
                            outcome = "cancelled"
                            return True
                        return False
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
                    backup_finished = perf_counter()
                    recorder.append(
                        "requests.jsonl",
                        dict(
                            id=1 << 31,
                            kind="backup",
                            scheduled_s=scheduled - origin,
                            started_s=entered - origin,
                            finished_s=backup_finished - origin,
                            outcome=outcome,
                        ),
                    )

            operations.append(
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
                )
            )
            if backup_enabled:
                operations.append(asyncio.create_task(backup()))

            def stop_schedule():
                nonlocal timed_out
                timed_out = True
                errors.append("measurement_maximum_exceeded")
                for task in operations:
                    task.cancel()

            def sampler_done(task):
                if not task.cancelled() and task.exception() is not None:
                    diagnostics.append("".join(traceback.format_exception(task.exception())))
                    errors.append("measurement_sampler_failed")
                    for operation in operations:
                        operation.cancel()

            sampling = asyncio.create_task(sample())
            sampling.add_done_callback(sampler_done)
            deadline = loop.call_later(run["load"]["maximum_seconds"], stop_schedule)
            results = await asyncio.gather(*operations)
            await wall_until(start + run["load"]["window_seconds"])
            completed = all(results)
            if backup_enabled and (
                backup_finished is None
                or last_business_finished is None
                or backup_finished > start + run["load"]["window_seconds"]
                or backup_finished > last_business_finished
                or len(child_exit_times) != len(processes)
                or any(at > backup_finished for at in child_exit_times.values())
            ):
                completed = False
                errors.append("measurement_backup_window_uncovered")
        except asyncio.CancelledError:
            cancelled = not errors and not timed_out
        except BaseException as exc:
            diagnostics.append("".join(traceback.format_exception(exc)))
            errors.append("measurement_trial_operation_failed")
        finally:
            if deadline is not None:
                deadline.cancel()
            for task in operations:
                if not task.done():
                    task.cancel()
            await _drain(asyncio.gather(*operations, return_exceptions=True), errors)
            if planned_cancel is not None:
                await _drain(planned_cancel.settle(), errors)
            if workers is not None:
                await _drain(workers.stop("wait" if completed and not errors else "cancel"), errors)
            if sampling is not None:
                sampling.cancel()
                await _drain(sampling, errors, cancelled_ok=True)
            if owner is not None:

                async def final_bytes():
                    metrics["backup_bytes"] = await _thread(
                        lambda: file_bytes(owner.home / "backups/regular", suffix=".clytie-backup")
                    )

                await _drain(final_bytes(), errors)
                await _drain(fixture.close(owner), errors)
                if owner.storage_owner is not None:
                    if owner.storage_owner._thread.is_alive():
                        errors.append("measurement_owner_exit_unconfirmed")
                    else:
                        release(1, "thread", "exit_verified")
                        if control is not None:
                            budget = control.budget.status()
                            if budget["active"] or budget["waiting"]:
                                errors.append("measurement_budget_not_released")
                            await _drain(control.ended(session, confirmed=True), errors)
            ticking = False
            if tick_handle is not None:
                tick_handle.cancel()
            if views is not None:
                try:
                    views.verify_released()
                except BaseException:
                    errors.append("measurement_view_release_unconfirmed")
            for index, child in enumerate(processes, 1):
                if child.returncode is None:
                    errors.append("measurement_worker_exit_unconfirmed")
                else:
                    if index not in child_exit_times:
                        child_exit_times[index] = perf_counter()
                        release(index, "process", "exit_verified")
                    if child.returncode != 0:
                        errors.append("measurement_worker_failed")
            try:
                instruments.close()
            except BaseException:
                recorder.failed = True
                errors.append("measurement_observer_unsettled")
    cancelled = cancelled or "measurement_cleanup_wait_cancelled" in errors
    result = "cancelled" if cancelled else "pass" if completed and not errors else "fail"
    if scenario == "cancel" and completed and not errors:
        result = "cancelled"
    manifest = {**run, "metrics": metrics, "result": result}
    if diagnostics:
        # Local diagnosis only, outside the raw whitelist. Never package an
        # arbitrary exception as approved structured fictional evidence.
        with (root / "trial-diagnostics.log").open("x", encoding="utf-8") as stream:
            stream.write("\n".join(diagnostics))
    with (root / "trial-status.json").open("x", encoding="utf-8") as stream:
        json.dump(
            dict(
                codes=errors,
                window_seconds=run["load"]["window_seconds"],
                planned_end_s=None
                if start is None
                else start - origin + run["load"]["window_seconds"],
                backup_finished_s=None if backup_finished is None else backup_finished - origin,
                last_business_finished_s=None
                if last_business_finished is None
                else last_business_finished - origin,
            ),
            stream,
            separators=(",", ":"),
        )
    try:
        for index, _ in enumerate(processes, 1):
            name = (
                f"child-storage-phases-{index}.jsonl"
                if run["format"] in {5, 6}
                else "child-storage-phases.jsonl"
            )
            recorder.append_bytes("storage-phases.jsonl", (root / name).read_bytes())
        verify(product_root.parent)
        verify_imports(product_root)
        recorder.finish(root / "raw", manifest)
    except BaseException:
        _preserve_invalid(root, recorder, manifest)
        raise TrialError(
            "measurement_trial_evidence_invalid", {**manifest, "result": "incomplete"}
        ) from None
    if cancelled:
        raise asyncio.CancelledError()
    return manifest
