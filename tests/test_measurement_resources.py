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
