"""Instrument only the real backup child of a marked fictional measurement.

The parent launches this instead of ``-m clytie.backup.worker`` with the same
interpreter, environment and control pipes. Product dispatch and cancellation
are unchanged. Missing/truncated evidence is an incomplete run, never success.
"""

import math
import os
import sys
from pathlib import Path

try:
    from . import measurement_evidence as evidence
    from .measurement_observation import Observation
    from .measurement_recording import Recorder
except ImportError:
    import measurement_evidence as evidence
    from measurement_observation import Observation
    from measurement_recording import Recorder


def main(arguments=None):
    args = sys.argv[1:] if arguments is None else arguments
    try:
        if len(args) not in {3, 4, 6}:
            raise ValueError()
        root = evidence._safe_path(Path(args[0]).absolute())
        marker = evidence._safe_path(root / "measurement-fixture.json")
        if marker.stat().st_size > 1024 or evidence._json(marker.read_bytes()) != {
            "format": 1,
            "source": "generated_fictional_only",
        }:
            raise ValueError()
        home = evidence._safe_path(root / "home")
        if not home.is_dir():
            raise ValueError()
        operation, origin = args[1], float(args[2])
        format_version = int(args[3]) if len(args) >= 4 else 1
        if format_version not in {1, 2, 3, 4, 5} or (format_version == 5) != (len(args) == 6):
            raise ValueError()
        evidence._pattern(operation, r"[a-f0-9]{32}")
        if not math.isfinite(origin) or origin < 0:
            raise ValueError()
        target = root / "child-storage-phases.jsonl"
        process_sequence = None
        if format_version == 5:
            evidence._pattern(args[4], r"[1-9][0-9]{0,2}")
            process_sequence = int(args[4])
            evidence._pattern(args[5], r"[a-f0-9]{64}")
            plan_path = evidence._safe_path(root / "measurement-observers.json")
            if plan_path.stat().st_size > evidence.MAX_LINE:
                raise ValueError()
            plan = evidence._json(plan_path.read_bytes())
            evidence._fields(plan, ("format", "execution_id", "observer_sequences"))
            if (
                type(plan["format"]) is not int
                or plan["format"] != 1
                or plan["execution_id"] != args[5]
            ):
                raise ValueError()
            evidence.validate_observer_sequences(plan["observer_sequences"])
            if process_sequence not in plan["observer_sequences"]:
                raise ValueError()
            target = root / f"child-storage-phases-{process_sequence}.jsonl"
        if target.exists() or target.is_symlink():
            raise ValueError()
        # Reserve the exact output before starting a v5 child. Two launchers
        # cannot execute under the same frozen ordinal and overwrite evidence.
        output = target.open("xb") if format_version == 5 else None
    except (OSError, ValueError, evidence.EvidenceError):
        print("measurement_worker_input_invalid", file=sys.stderr)
        return 2
    from clytie.backup import worker

    recorder = Recorder(format_version=format_version)
    observer = Observation(root, recorder, origin=origin, process_sequence=process_sequence)
    # A fixed disjoint namespace keeps child rows distinguishable after exact
    # byte concatenation; it is not a process identifier or filesystem path.
    if format_version != 5:
        observer.sequence = 1 << 40
    previous = sys.argv
    try:
        sys.argv = ["clytie.backup.worker", str(home), operation]
        with observer.install():
            worker.main()
    finally:
        sys.argv = previous
        try:
            if recorder.failed:
                raise evidence.EvidenceError("child_recording_invalid")
            raw = bytes(recorder.rows["storage-phases.jsonl"])
            if not raw:
                raise evidence.EvidenceError("child_recording_missing")
            # Partial output after a kill remains incomplete evidence.
            if output is None:
                output = target.open("xb")
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        finally:
            if output is not None:
                output.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
