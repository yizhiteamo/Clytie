import subprocess
import sys

import pytest
from tests.test_measurement_evidence import load_tool

resources = load_tool("measurement_resources")


def test_real_parent_and_owned_child_rss_requires_matching_process_identity():
    from clytie.plugin_host.locking import process_identity

    assert resources.rss_bytes(process_identity()) > 0
    child = subprocess.Popen(
        [sys.executable, "-c", "import sys; print('ready', flush=True); sys.stdin.buffer.read()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    try:
        assert child.stdout.readline().strip() == b"ready"
        identity = process_identity(child.pid)
        assert resources.rss_bytes(identity) > 0
        with pytest.raises(ValueError, match="identity_changed"):
            resources.rss_bytes({**identity, "started_at": "fictional mismatch"})
    finally:
        child.stdin.close()
        child.wait(timeout=10)
        child.stdout.close()
    with pytest.raises((OSError, ProcessLookupError)):
        resources.rss_bytes(identity)


@pytest.mark.parametrize("error", [FileNotFoundError, PermissionError])
def test_tree_sample_handles_only_removed_directory(tmp_path, monkeypatch, error):
    moving = tmp_path / "moving"
    moving.mkdir()
    (tmp_path / "source-wal").write_bytes(b"fictional")
    original = resources.os.scandir

    def scan(path):
        if resources.Path(path) == moving:
            raise error("fictional traversal race")
        return original(path)

    monkeypatch.setattr(resources.os, "scandir", scan)
    if error is PermissionError:
        with pytest.raises(PermissionError):
            resources.file_bytes(tmp_path, suffix="-wal")
    else:
        assert resources.file_bytes(tmp_path, suffix="-wal") == len(b"fictional")
