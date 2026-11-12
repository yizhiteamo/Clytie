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
        if len(args) != 3:
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
        evidence._pattern(operation, r"[a-f0-9]{32}")
        if not math.isfinite(origin) or origin < 0:
            raise ValueError()
        target = root / "child-storage-phases.jsonl"
        if target.exists() or target.is_symlink():
            raise ValueError()
    except (OSError, ValueError, evidence.EvidenceError):
        print("measurement_worker_input_invalid", file=sys.stderr)
        return 2
    from clytie.backup import worker

    recorder = Recorder()
    observer = Observation(root, recorder, origin=origin)
    # A fixed disjoint namespace keeps child rows distinguishable after exact
    # byte concatenation; it is not a process identifier or filesystem path.
    observer.sequence = 1 << 40
    previous = sys.argv
    try:
        sys.argv = ["clytie.backup.worker", str(home), operation]
        with observer.install():
            worker.main()
    finally:
        sys.argv = previous
        if recorder.failed:
            raise evidence.EvidenceError("child_recording_invalid")
        raw = bytes(recorder.rows["storage-phases.jsonl"])
        if not raw:
            raise evidence.EvidenceError("child_recording_missing")
        # No destructive cleanup. Partial output after a kill remains invalid
        # evidence and cannot be converted into a completed measurement.
        with target.open("xb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
