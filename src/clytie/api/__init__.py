"""Local, read-only status facade used by the A1 command-line shell."""

import json
from dataclasses import replace
from pathlib import Path

from clytie.config import HomeReport, load_home, safety_belt_settings
from clytie.kernel.query import local_task, task_exit_code


def local_status(home: str | Path | None = None, *, repo_root: Path | None = None) -> HomeReport:
    """Validate an instance locally; no runtime services are started or contacted."""
    from clytie.kernel.migration import migration_status, snapshot_notices
    from clytie.plugin_host.recovery import failed_status

    report = load_home(home, repo_root=repo_root)
    from clytie.clock import SystemClock
    from clytie.memory.status import notices as memory_notices

    notices = [*report.dialogue_notices, *memory_notices(report, clock=SystemClock())]
    from clytie.supervisor.common import read

    try:
        health = (
            read(Path(report.home) / "run/health.json", {}).get("companions", {})
            if report.exists
            else {}
        )
    except (OSError, ValueError, AttributeError):
        health = {}
    companions = []
    for companion in report.companions:
        runtime = health.get(companion["id"], {}) if isinstance(health, dict) else {}
        effective = report.plugin_configs.get(companion["id"])
        companions.append(
            {
                **companion,
                **(
                    {"safety_belt_config": safety_belt_settings(effective["config"])}
                    if effective and companion["valid"]
                    else {}
                ),
                **{
                    key: runtime[key]
                    for key in ("safety_belt", "attention", "storage_observed_at")
                    if key in runtime
                },
            }
        )
        path = Path(report.home) / "companions" / companion["id"] / "boundary/state.json"
        if path.is_file():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                for warning in state.get("warnings", []):
                    if warning in {
                        "boundary_notifier_unavailable",
                        "boundary_notification_failed",
                        "task_card_private_name",
                    }:
                        notices.append({"companion_id": companion["id"], "code": warning})
            except (OSError, ValueError, AttributeError):
                notices.append(
                    {"companion_id": companion["id"], "code": "boundary_state_unreadable"}
                )
    return replace(
        report,
        companions=tuple(companions),
        dialogue_notices=tuple(notices),
        failed_plugin_state=failed_status(report.home),
        ledger_errors=tuple(migration_status(report.home)),
        ledger_notices=tuple(snapshot_notices(report.home)),
    )


__all__ = ["local_status", "local_task", "task_exit_code"]


def local_supervisor_status(home=None):
    from clytie.config import resolve_home
    from clytie.supervisor.entry import status

    return status(resolve_home(home))


def local_supervisor_start(home=None, *, maintenance=False):
    from clytie.config import resolve_home
    from clytie.supervisor.entry import frontend

    return frontend(resolve_home(home), maintenance=maintenance)
