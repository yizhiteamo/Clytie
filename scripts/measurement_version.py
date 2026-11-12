"""Fixed product trees in fresh processes; measurement adapters stay separate.

No rewriting old production code, no network, no checkout/reset of the working
branch. Every extracted regular file is checked against its retained inventory.
"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path, PurePosixPath

LEGACY = "05ec93c7c4ae972b7475911b0aec9db57bb00390"
ORIGINAL = "b40bbbc31cfb2ff012a151d0e0e9c79acf07d903"


def _git(repository, *arguments):
    return subprocess.check_output(["git", *arguments], cwd=repository)


def prepare(repository, directory, *, mode, candidate):
    if mode not in {"legacy", "original", "candidate"}:
        raise ValueError("measurement_version_invalid")
    if len(candidate) != 40 or any(c not in "0123456789abcdef" for c in candidate):
        raise ValueError("measurement_candidate_invalid")
    if _git(repository, "rev-parse", "HEAD").decode().strip() != candidate:
        raise ValueError("measurement_candidate_changed")
    commit = {"legacy": LEGACY, "original": ORIGINAL, "candidate": candidate}[mode]
    archive = _git(repository, "archive", "--format=tar", commit)
    directory = Path(directory).absolute()
    if directory.exists() or directory.is_symlink():
        raise ValueError("measurement_version_exists")
    directory.mkdir(parents=True)
    source = directory / "source"
    source.mkdir()
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
        for entry in bundle:
            name = PurePosixPath(entry.name)
            if name.is_absolute() or ".." in name.parts or "\\" in entry.name or ":" in entry.name:
                raise ValueError("measurement_archive_path")
            target = source.joinpath(*name.parts)
            if entry.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif entry.isfile():
                if entry.name in files:
                    raise ValueError("measurement_duplicate_file")
                target.parent.mkdir(parents=True, exist_ok=True)
                data = bundle.extractfile(entry).read()
                with target.open("xb") as output:
                    output.write(data)
                files[entry.name] = hashlib.sha256(data).hexdigest()
            else:
                raise ValueError("measurement_archive_entry")
    inventory = {
        "format": 1,
        "mode": mode,
        "candidate": candidate,
        "source_commit": commit,
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "files": files,
    }
    raw = json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
    (directory / "source-inventory.json").write_bytes(raw)
    verify(directory)
    return inventory


def verify(directory):
    directory = Path(directory).resolve(strict=True)
    inventory = json.loads((directory / "source-inventory.json").read_bytes())
    source = directory / "source"
    found = {}
    for root, directories, files in os.walk(source, followlinks=False):
        for name in (*directories, *files):
            path = Path(root) / name
            if path.is_symlink() or getattr(path.lstat(), "st_file_attributes", 0) & 0x400:
                raise ValueError("measurement_source_link")
        for name in files:
            path = Path(root) / name
            # The child is launched with bytecode disabled. Extra files, including
            # caches, invalidate the inventory rather than being silently ignored.
            found[path.relative_to(source).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    if found != inventory["files"]:
        raise ValueError("measurement_source_changed")
    return inventory


def activate(directory):
    if any(name == "clytie" or name.startswith("clytie.") for name in sys.modules):
        raise ValueError("measurement_fresh_process_required")
    inventory = verify(directory)
    source = Path(directory).resolve(strict=True) / "source"
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source / "src"))
    # Backup children inherit this exact product import path; their executable
    # wrapper is the separately identified measurement tool, not another product.
    os.environ["PYTHONPATH"] = str(source / "src")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    import clytie

    if Path(clytie.__file__).resolve().parent != source / "src/clytie":
        raise ValueError("measurement_wrong_product")
    return source, inventory


def verify_imports(source):
    root = Path(source).resolve() / "src/clytie"
    for name, module in tuple(sys.modules.items()):
        if name == "clytie" or name.startswith("clytie."):
            location = getattr(module, "__file__", None)
            if not location or not Path(location).resolve().is_relative_to(root):
                raise ValueError("measurement_mixed_product")
