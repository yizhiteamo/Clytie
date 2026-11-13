"""Bind a tiny trial to inventoried code, fixture and tool bytes in a fresh process.

The formal mechanism/matrix launcher is not enabled by this fixture command.
No upload, no model other than the selected product's FakeModel, no retries.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import sqlite3
from pathlib import Path

try:
    from . import measurement_evidence as evidence
    from .measurement_version import LEGACY, ORIGINAL, activate, verify, verify_imports
except ImportError:
    import measurement_evidence as evidence
    from measurement_version import LEGACY, ORIGINAL, activate, verify, verify_imports

ROOT = Path(__file__).resolve().parents[1]
TOOL_FILES = (
    ".pre-commit-config.yaml",
    "pyproject.toml",
    "uv.lock",
    "scripts/measurement_evidence.py",
    "scripts/measurement_analysis_index.py",
    "scripts/measurement_mechanism_validation.py",
    "scripts/measurement_cancel.py",
    "scripts/measurement_cancel_validation.py",
    "scripts/measurement_fixture.py",
    "scripts/measurement_geometry.py",
    "scripts/measurement_preparation_evidence.py",
    "scripts/measurement_preparation_transport.py",
    "scripts/measurement_preparation_generate.py",
    "scripts/measurement_preparation_request.py",
    ".github/workflows/preparation.yml",
    "scripts/measurement_launch.py",
    "scripts/measurement_load.py",
    "scripts/measurement_io_observation.py",
    "scripts/measurement_control_observation.py",
    "scripts/measurement_budget_observation.py",
    "scripts/measurement_batch_observation.py",
    "scripts/measurement_batch_validation.py",
    "scripts/measurement_pressure_validation.py",
    "scripts/measurement_pressure_observation.py",
    "scripts/measurement_observation.py",
    "scripts/measurement_native_observation.py",
    "scripts/measurement_recording.py",
    "scripts/measurement_plan.py",
    "scripts/measurement_mechanism_plan.py",
    "scripts/measurement_mechanism_launch.py",
    "scripts/measurement_sample_validation.py",
    "scripts/measurement_resources.py",
    "scripts/measurement_transport.py",
    "scripts/measurement_trial.py",
    "scripts/measurement_version.py",
    "scripts/measurement_views.py",
    "scripts/measurement_worker.py",
)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def tool_inventory(root=ROOT):
    files = {}
    for name in TOOL_FILES:
        path = evidence._safe_path(Path(root) / name)
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"files": files, "sha256": hashlib.sha256(canonical(files)).hexdigest()}


def environment():
    return dict(
        cpu_count=os.cpu_count(),
        os=platform.system(),
        release_sha256=hashlib.sha256(platform.release().encode()).hexdigest(),
        machine_sha256=hashlib.sha256(platform.machine().encode()).hexdigest(),
    )


def tiny_manifest(inventory, tools, arm, scale, *, format_version=4):
    if format_version not in {4, 5, 6}:
        raise ValueError("measurement_format_invalid")
    ledger = dict.fromkeys(
        ("inbound", "record", "invocation", "intent", "attempt", "dedupe", "audit"), 0
    )
    asset = scale == "large_asset"
    files = 1 if asset else 2
    logical = dict(
        records=8,
        revisions=8,
        content_bytes=8 * 4096,
        content_bytes_per_record=4096,
        content_length_distribution=dict(
            min_bytes=4096, max_bytes=4096, total_bytes=8 * 4096, records=8
        ),
        file_count=files,
        file_bytes=files * 32,
        provider_files=0 if asset else files,
        provider_bytes=0 if asset else files * 32,
        asset_registrations=1 if asset else 0,
        asset_bytes=32 if asset else 0,
        ledger_rows=ledger,
    )
    order = ["baseline", "none", "original", "candidate"]
    recipe = dict(
        format=1,
        purpose="tiny_adapter_check_only",
        window_seconds=6,
        interval_seconds=0.025,
        request_count=240,
        maximum_seconds=30,
        backup_start_seconds=0.1,
        warmup_requests=2,
    )
    run = dict(
        format=format_version,
        source="generated_fictional_only",
        method="legal_fixture_comparison_v1",
        candidate=inventory["candidate"],
        reference=ORIGINAL,
        baseline_reference=LEGACY,
        source_commit=inventory["source_commit"],
        run_id=1,
        job_id="local_fixture",
        attempt=1,
        platform=platform.system(),
        seed=73,
        scale=scale,
        group=1,
        arm=arm,
        phase="fixture",
        plan_sha256=hashlib.sha256(canonical(recipe)).hexdigest(),
        parameters=dict(evidence.PARAMETERS),
        load={
            name: recipe[name]
            for name in (
                "window_seconds",
                "interval_seconds",
                "request_count",
                "maximum_seconds",
                "backup_start_seconds",
            )
        },
        fixture_version="legal_memory_v2",
        fixture_sha256=tools["files"]["scripts/measurement_fixture.py"],
        tool_sha256=tools["sha256"],
        execution=dict(
            order=order, position=order.index(arm), warmup_requests=recipe["warmup_requests"]
        ),
        environment=environment(),
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
        metrics=dict(
            logical=logical,
            ledger_rows=ledger,
            memory_database_bytes=0,
            ledger_database_bytes=0,
            asset_file_bytes=32 if asset else 0,
            plugin_file_bytes=0 if asset else files * 32,
            backup_bytes=0,
        ),
        result="incomplete",
    )
    if format_version in {5, 6}:
        run.update(
            observer_sequences=[0, 1] if arm in {"candidate", "original"} else [0],
            scenario="normal",
            execution_id=hashlib.sha256(
                canonical(
                    dict(
                        candidate=inventory["candidate"],
                        arm=arm,
                        scale=scale,
                        recipe=recipe,
                        tool=tools["sha256"],
                    )
                )
            ).hexdigest(),
        )
    evidence.validate_run(run)
    return run, recipe


def main(arguments=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--arm", choices=("baseline", "none", "original", "candidate"), required=True
    )
    parser.add_argument("--scale", choices=("small_text", "large_asset"), required=True)
    parser.add_argument("--observation-format", type=int, choices=(4, 5, 6), default=4)
    args = parser.parse_args(arguments)
    source, inventory = activate(args.version_directory)
    tools = tool_inventory()
    run, recipe = tiny_manifest(
        inventory, tools, args.arm, args.scale, format_version=args.observation_format
    )
    from measurement_trial import run as trial

    # These pre-call files remain even when the child, observation or cleanup fails.
    evidence._safe_path(args.output.parent.absolute())
    before = args.output.parent / (args.output.name + "-before.json")
    with before.open("xb") as output:
        output.write(canonical(dict(expected=run, recipe=recipe, tools=tools)))
    try:
        actual = asyncio.run(trial(args.output, run, source, tiny=True))
    finally:
        verify_imports(source)
        verify(args.version_directory)
        if tool_inventory() != tools:
            raise ValueError("measurement_tools_changed")
    evidence.validate_files(
        {name: (args.output / "raw" / name).read_bytes() for name in evidence.FILES}, run
    )
    print(json.dumps(dict(fixture_check_complete=True, result=actual["result"])))


if __name__ == "__main__":
    main()
