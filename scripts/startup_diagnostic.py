"""Run the approved six-case diagnostic through Python's module entry point."""

import argparse
import subprocess
import sys
from pathlib import Path

TARGETS = (
    "tests/test_backup_maintenance.py::"
    "test_real_maintenance_empty_or_invalid_home_never_starts_business",
    "tests/test_api_startup_read.py::test_read_denial_then_readiness_sends_one_ack",
    "tests/test_api_startup_read.py::test_other_read_io_errors_fail_explicitly_without_retry",
    "tests/test_api_startup_read.py::"
    "test_real_windows_read_denial_then_release_without_permission_changes",
)


def command(*, collect_only=False):
    result = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "tests.startup_diagnostic_plugin",
        "-q",
        "-s",
        "--tb=no",
    ]
    if collect_only:
        result.append("--collect-only")
    return [*result, *TARGETS]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    if Path.cwd().resolve() != root:
        parser.error("repository_root_required")
    # Module execution adds this fixed working directory before pytest loads -p.
    # No PYTHONPATH injection, dependency installation, retry or test alteration.
    return subprocess.run(command(collect_only=args.collect_only), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
