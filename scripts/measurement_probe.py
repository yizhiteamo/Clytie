"""Fresh-process, tiny three-version production-path check. Not a timed acceptance run.

Only the CLI imports the selected product, after checking the full fixed-tree
inventory. Eight legal records and three requests prove adapter correctness;
these numbers cannot be passed as formal workload parameters.
"""

import argparse
import asyncio
import hashlib
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from time import perf_counter  # noqa: TID251 -- fictional test observer
from types import SimpleNamespace
from unittest.mock import patch

try:
    from .measurement_version import activate, verify, verify_imports
except ImportError:
    from measurement_version import activate, verify, verify_imports


async def probe(directory, product, mode, *, scale):
    from measurement_observation import Observation
    from measurement_recording import Recorder

    root = Path(directory).absolute()
    recorder = Recorder()
    origin = perf_counter()
    observation = Observation(root.parent, recorder, origin=origin)
    # Install the connection observer before the selected product creates its
    # databases; installing it later cannot observe already-open connections.
    with observation.install():
        return await _probe(root, product, mode, scale, recorder, observation, origin)


async def _probe(root, product, mode, scale, recorder, observation, origin):
    import measurement_fixture as fixture
    from measurement_observation import request_scope
    from measurement_views import Views

    from clytie.backup.controller import CaptureBridge, capture_request
    from clytie.backup.workers import BackupWorkers
    from clytie.plugin_host.locking import process_identity
    from clytie.supervisor.common import Audit

    owner, metrics = await fixture.setup(root, scale, mode, 73, tiny=True, product_root=product)
    (root / "measurement-fixture.json").write_text(
        json.dumps(dict(format=1, source="generated_fictional_only")), encoding="utf-8"
    )
    views = Views(recorder, now=observation.now) if mode != "legacy" else None
    loop = asyncio.get_running_loop()
    previous_tick = origin
    handle = None
    running = True
    workers = control = session = None
    processes = []
    errors = []
    backup = None

    def tick():
        nonlocal previous_tick, handle
        now = perf_counter()
        owner.clock.advance(max(0, now - previous_tick))
        previous_tick = now
        if running:
            handle = loop.call_later(0.005, tick)

    tick()
    with ExitStack() as instruments:
        if owner.storage_owner is not None:
            instruments.enter_context(observation.observe_owner(owner.storage_owner))
        if views is not None:
            instruments.enter_context(views.install())
        try:
            if mode == "candidate":
                from clytie.backup.io_channel import IoChannel
                from clytie.backup.io_control import IoControl

                control = IoControl(owner.clock, home=owner.home)
                session = control.register(storage_owner=True)

                async def reply(message):
                    channel.receive(message)

                def emit(message):
                    loop.call_soon_threadsafe(lambda: control.dispatch(session, message, reply))

                channel = IoChannel(emit)
                owner.backup_io_client = channel.client
                control.pressure(session, owner.storage_owner.sample_io_pressure())

            async def request(cid, operation, arguments, **kwargs):
                if cid != owner.cid or operation != "backup_capture":
                    raise ValueError("measurement_unexpected_operation")
                before_send = kwargs.get("before_send")
                if before_send is not None:
                    await before_send(owner)
                return await capture_request(owner, arguments)

            if mode != "legacy":
                supervisor = SimpleNamespace(
                    children={owner.cid: owner},
                    rows={owner.cid: {"state": "running"}},
                    audit=Audit(owner.home, owner.clock),
                    request=request,
                )
                if mode == "candidate":

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
                native_spawn = asyncio.create_subprocess_exec
                wrapper = Path(__file__).with_name("measurement_worker.py")

                async def spawn(*args, **kwargs):
                    if tuple(args[1:3]) != ("-m", "clytie.backup.worker") or len(args) != 5:
                        raise ValueError("measurement_unexpected_child")
                    if Path(args[3]).resolve() != owner.home.resolve():
                        raise ValueError("measurement_child_outside_fixture")
                    child = await native_spawn(
                        args[0], str(wrapper), str(root), args[4], str(origin), **kwargs
                    )
                    processes.append(child)
                    return child

                instruments.enter_context(patch.object(asyncio, "create_subprocess_exec", spawn))

            for index in range(3):
                with request_scope(index + 1):
                    await fixture.business(owner, index)
            if workers is not None:
                operation = "d" * 32
                await workers.execute("create", {"scope": "instance"}, operation)
                await asyncio.shield(asyncio.gather(*tuple(workers.tasks.values())))
                backup = workers.query(operation)
                if backup["phase"] != "committed" or backup["result"]["creation"] != "complete":
                    # Fixed enum diagnostics, no arbitrary exception or product content.
                    raise ValueError("measurement_complete_backup_required")
                if views.pending:
                    raise ValueError("measurement_views_pending")
                views.verify_released()
        except BaseException as exc:
            errors.append(exc)
        finally:
            if workers is not None:
                try:
                    await workers.stop("cancel" if errors else "wait")
                except BaseException as exc:
                    errors.append(exc)
            try:
                await fixture.close(owner)
            except BaseException as exc:
                errors.append(exc)
            if control is not None:
                try:
                    await control.ended(
                        session, confirmed=not owner.storage_owner._thread.is_alive()
                    )
                except BaseException as exc:
                    errors.append(exc)
            running = False
            if handle:
                handle.cancel()
    if views:
        views.verify_released()
    if any(child.returncode != 0 for child in processes):
        raise ValueError("measurement_worker_exit_unconfirmed")
    if errors:
        raise errors[0]
    responsibility_verified = False
    if mode == "candidate":
        state = await control.recheck()
        ledger = json.loads((owner.home / "supervisor/io-responsibility/index.json").read_bytes())
        captures = [row for row in ledger["records"].values() if row["role"] == "capture"]
        responsibility_verified = bool(captures) and all(
            row["state"] == "ended" for row in captures
        )
        if state["state"] != "ready" or not responsibility_verified:
            raise ValueError("measurement_responsibility_unsettled")
    if workers is not None:
        recorder.append_bytes(
            "storage-phases.jsonl", (root / "child-storage-phases.jsonl").read_bytes()
        )
    phases = [json.loads(row) for row in recorder.rows["storage-phases.jsonl"].splitlines()]
    if not any(row["phase"] == "commit" for row in phases):
        raise ValueError("measurement_commit_observation_missing")
    # Preserve exact primitive observations separately from this functional summary.
    (root / "probe-storage-phases.jsonl").write_bytes(bytes(recorder.rows["storage-phases.jsonl"]))
    (root / "probe-release-events.jsonl").write_bytes(bytes(recorder.rows["release-events.jsonl"]))
    return dict(
        mode=mode,
        scale=scale,
        fixture_version=fixture.FIXTURE_VERSION,
        metrics=metrics,
        requests_completed=3,
        complete_backup=backup is not None,
        process_count=len(processes),
        processes_exited=all(child.returncode == 0 for child in processes),
        views_released=views is None or views.pending == 0,
        owner_exited=owner.storage_owner is None or not owner.storage_owner._thread.is_alive(),
        responsibility_verified=responsibility_verified,
        observed_commits=sum(row["phase"] == "commit" for row in phases),
    )


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scale", choices=("small_text", "large_asset"), required=True)
    args = parser.parse_args(arguments)
    source, inventory = activate(args.version_directory)
    # Script mode and package mode use the same separately inventoried adapter tree.
    sys.path.insert(1, str(Path(__file__).resolve().parent))
    result = asyncio.run(probe(args.output, source, inventory["mode"], scale=args.scale))
    verify_imports(source)
    verify(args.version_directory)
    result.update(
        source_commit=inventory["source_commit"],
        candidate=inventory["candidate"],
        inventory_sha256=hashlib.sha256(
            (args.version_directory / "source-inventory.json").read_bytes()
        ).hexdigest(),
    )
    (args.output / "probe-result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print("measurement_production_probe_passed")


if __name__ == "__main__":
    main()
