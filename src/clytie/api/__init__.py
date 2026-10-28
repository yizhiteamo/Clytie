"""Local, read-only status facade used by the A1 command-line shell."""

from pathlib import Path

from clytie.config import HomeReport, load_home


def local_status(home: str | Path | None = None, *, repo_root: Path | None = None) -> HomeReport:
    """Validate an instance locally; no runtime services are started or contacted."""
    return load_home(home, repo_root=repo_root)


__all__ = ["local_status"]
