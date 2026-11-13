"""Fresh-process geometry preparation; no benchmark, uploader or key reader."""

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

try:
    from . import measurement_evidence as evidence
    from . import measurement_launch as launch
    from . import measurement_preparation_evidence as prep
    from . import measurement_preparation_transport as transport
    from . import measurement_version as versions
except ImportError:
    import measurement_evidence as evidence
    import measurement_launch as launch
    import measurement_preparation_evidence as prep
    import measurement_preparation_transport as transport
    import measurement_version as versions

ROOT = Path(__file__).resolve().parents[1]


def runtime():
    return dict(
        environment=launch.environment(),
        python=platform.python_version(),
        sqlite=sqlite3.sqlite_version,
    )


def current(root, candidate):
    evidence._pattern(candidate, r"[a-f0-9]{40}")
    if versions._git(root, "rev-parse", "HEAD").decode().strip() != candidate:
        raise prep.Error("preparation_candidate_changed")
    if versions._git(root, "diff", "HEAD", "--name-only").strip():
        raise prep.Error("preparation_worktree_modified")


async def build(version_directory, target):
    # Must be a fresh interpreter: activation refuses already imported product code.
    source, inventory = versions.activate(version_directory)
    geometry = importlib.import_module(
        "measurement_geometry" if not __package__ else ".measurement_geometry", __package__
    )
    before = versions.verify(version_directory)
    if before != inventory:
        raise prep.Error("preparation_source_changed")
    await geometry.construct(target, tuple(geometry.fixture.SCALES), seed=73)
    versions.verify_imports(source)
    if versions.verify(version_directory) != before:
        raise prep.Error("preparation_source_changed")
    prep._save_bytes(Path(target) / "runtime.json", prep.canonical(runtime()))


def prepare(root, target, candidate, *, environ=None, execute=subprocess.run):
    env = os.environ if environ is None else environ
    current(root, candidate)
    if env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != "yizhiteamo/Clytie":
        raise prep.Error("preparation_ci_identity_invalid")
    run_id, attempt = int(env["GITHUB_RUN_ID"]), int(env["GITHUB_RUN_ATTEMPT"])
    platform_name = platform.system()
    if env["GITHUB_JOB"] != "prepare_" + platform_name.lower():
        raise prep.Error("preparation_job_invalid")
    tool = launch.tool_inventory(root)
    if "scripts/measurement_preparation_generate.py" not in tool["files"]:
        raise prep.Error("preparation_generator_not_inventoried")
    target = prep._new(target)
    version_directory = target / "product"
    versions.prepare(root, version_directory, mode="candidate", candidate=candidate)
    before = transport.material_identity(root, version_directory, candidate)
    geometry_dir = target / "fictional-scales"
    child = execute(
        [
            sys.executable,
            str(Path(root) / "scripts/measurement_preparation_generate.py"),
            "build",
            "--version-directory",
            str(version_directory),
            "--target",
            str(geometry_dir),
        ],
        cwd=root,
        env={**env, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    # Local/ephemeral diagnostic only; never in the approved raw/upload whitelist.
    prep._save_bytes(target / "generation-diagnostic.txt", child.stdout)
    if child.returncode:
        raise prep.Error("preparation_generation_failed")
    current(root, candidate)
    if (
        transport.material_identity(root, version_directory, candidate) != before
        or launch.tool_inventory(root) != tool
    ):
        raise prep.Error("preparation_material_changed")
    binding = dict(
        candidate=candidate,
        **before,
        fixture_version="legal_memory_v2",
        seed=73,
        platform=platform_name,
        run_id=run_id,
        job_id=env["GITHUB_JOB"],
        attempt=attempt,
    )
    raw = prep._new(target / "raw")
    for name in prep.FILES:
        prep._save_bytes(raw / name, prep._read_file(geometry_dir / name, prep.MAX_FILE))
    contents = prep._read_sources(raw)
    prep.validate_files(contents, binding)
    # Bind actual runtime rather than trusting version labels supplied by the file.
    if json.loads(contents["runtime.json"]) != runtime():
        raise prep.Error("preparation_runtime_changed")
    prep._save_bytes(target / "expected.json", prep.canonical(binding))
    prep._save_bytes(
        target / "generation.json",
        prep.canonical(
            dict(
                purpose=prep.PURPOSE,
                source_before=before,
                source_after=before,
                files={name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
                process_exit=child.returncode,
                measurement_acceptance="not_applicable",
            )
        ),
    )
    return binding


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    construct = commands.add_parser("build")
    construct.add_argument("--version-directory", type=Path, required=True)
    construct.add_argument("--target", type=Path, required=True)
    prepare_command = commands.add_parser("prepare")
    prepare_command.add_argument("--target", type=Path, required=True)
    prepare_command.add_argument("--candidate", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            asyncio.run(build(args.version_directory, args.target))
        else:
            prepare(ROOT, args.target, args.candidate)
        print("preparation_only_not_measurement")
        return 0
    except Exception:
        print("preparation_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
