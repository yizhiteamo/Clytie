"""Local, read-only status facade used by the A1 command-line shell."""

from dataclasses import replace
from pathlib import Path

from clytie.config import HomeReport, load_home


def local_status(home: str | Path | None = None, *, repo_root: Path | None = None) -> HomeReport:
    """Validate an instance locally; no runtime services are started or contacted."""
    from clytie.plugin_host.recovery import failed_status

    report = load_home(home, repo_root=repo_root)
    return replace(report, failed_plugin_state=failed_status(report.home))


__all__ = ["local_status"]
