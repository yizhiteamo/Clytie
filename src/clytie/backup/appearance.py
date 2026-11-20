"""Reference-list and description capture at the companion's asset registration boundary."""

import stat
from contextlib import nullcontext
from pathlib import Path

from clytie.plugin_host.snapshots import contained

from .io_client import heavy_io, io_read


def identities(home, cid):
    from .source import SourceError, _file_version

    result = {}
    for filename in ("appearance.md", "appearance.yaml"):
        name = f"companions/{cid}/persona/{filename}"
        path = contained(home, home / name)
        try:
            value = path.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
            raise SourceError("snapshot_inconsistent")
        result[name] = _file_version(value)
    return result


@heavy_io
def references(root, cid, *, check=None):
    path = contained(root, Path(root) / f"companions/{cid}/persona/appearance.yaml")
    if not path.exists():
        return {}
    from clytie.appearance import parse_manifest

    # Past lists are preserved verbatim, but their removed assets may have been released.
    parts = []
    with path.open("rb") as stream:
        while chunk := io_read(stream, 1024 * 1024):
            if check:
                check()
            parts.append(chunk)
    rows = parse_manifest(b"".join(parts))["references"]
    return {row["asset"]["asset_id"]: row["asset"]["size"] for row in rows}


def prepare(home, cid, work, clock, check):
    from .source import _copy

    versions = identities(home, cid)
    if not versions:
        return {}, [], {}
    entries = []
    for name, version in versions.items():
        lock = nullcontext()
        if name.endswith("/appearance.yaml"):
            from clytie.appearance import AppearanceFiles

            # On Windows, replacing a manifest with an open reader can fail. Only
            # management edits share this lock; the business capture gate is still free.
            lock = AppearanceFiles(home, cid, clock).edit_lock().commit()
        with lock:
            entries.append(_copy(home, home / name, work, name, check, expected_version=version))
    return versions, entries, references(work, cid, check=check)


def verify_versions(home, cid, versions):
    from .source import SourceError

    if identities(home, cid) != versions:
        raise SourceError("snapshot_inconsistent")


def validate_restored(root, cid, assets, *, check=None):
    refs = references(root, cid, check=check)
    if not refs:
        return
    owned = {
        asset["asset_id"]: asset["size"]
        for asset in assets
        if cid in asset["references"]
        and any(row["companion_id"] == cid for row in asset["registrations"])
    }
    if any(owned.get(asset_id) != size for asset_id, size in refs.items()):
        raise ValueError("restore_asset_unauthorized")
