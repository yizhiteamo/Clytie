"""只在 pytest 临时实例目录验证加密、校验、留存与恢复。"""

import json

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from clytie.plugin_host.snapshots import SnapshotError, UpgradeSnapshots
from clytie.plugin_host.support import HostLog
from clytie.sdk.testing import MockClock


def test_encrypted_snapshot_key_lifecycle_retention_and_restore(tmp_path):
    clock, log = MockClock(), HostLog()
    manager = UpgradeSnapshots(tmp_path, clock, log)
    state = manager.state_path("companion_a", "test-plugin")
    state.mkdir(parents=True)
    (state / "value.txt").write_text("fictional-original", encoding="utf-8")
    (state / "empty").mkdir()
    paths = []
    for index in range(4):
        paths.append(manager.create("companion_a", "test-plugin", "1.0.0", f"1.0.{index + 1}"))
        clock.advance(1)
    assert not paths[0].exists() and all(p.exists() for p in paths[1:])
    assert len(list(paths[-1].parent.glob("*.enc"))) == 3
    assert b"fictional-original" not in paths[-1].read_bytes()
    assert len((tmp_path / "secrets/backup.key").read_bytes()) == 32
    assert len([entry for entry in log.entries if entry[1].startswith("backup.key_created")]) == 1
    assert (
        "secrets/backup.key" not in manager.read(paths[-1], "companion_a", "test-plugin")["files"]
    )
    (state / "value.txt").write_text("fictional-changed", encoding="utf-8")
    (state / "new.txt").write_text("new fictional artifact", encoding="utf-8")
    manager.restore(paths[-1], "companion_a", "test-plugin")
    assert (state / "value.txt").read_text(encoding="utf-8") == "fictional-original"
    assert not (state / "new.txt").exists() and (state / "empty").is_dir()


@pytest.mark.parametrize("problem", ["key", "ciphertext", "header", "identity"])
def test_wrong_key_corruption_or_wrong_plugin_never_modifies_state(tmp_path, problem):
    manager = UpgradeSnapshots(tmp_path, MockClock(), HostLog())
    state = manager.state_path("companion_a", "test-plugin")
    state.mkdir(parents=True)
    (state / "value.txt").write_bytes(b"fictional-original")
    snapshot = manager.create("companion_a", "test-plugin", "1.0.0", "2.0.0")
    if problem == "key":
        (tmp_path / "secrets/backup.key").write_bytes(AESGCM.generate_key(bit_length=256))
    else:
        envelope = json.loads(snapshot.read_bytes())
        if problem == "ciphertext":
            envelope["ciphertext"] = "AAAA" + envelope["ciphertext"][4:]
        elif problem == "header":
            envelope["header"]["from"] = "7.0.0"
        snapshot.write_text(json.dumps(envelope), encoding="utf-8")
    (state / "value.txt").write_bytes(b"fictional-current")
    with pytest.raises(SnapshotError):
        manager.restore(
            snapshot, "companion_a", "other-plugin" if problem == "identity" else "test-plugin"
        )
    assert (state / "value.txt").read_bytes() == b"fictional-current"


def test_symlink_state_and_invalid_key_are_not_followed_or_replaced(tmp_path):
    manager = UpgradeSnapshots(tmp_path, MockClock(), HostLog())
    path = tmp_path / "secrets/backup.key"
    path.parent.mkdir()
    path.write_bytes(b"fictional-invalid-key")
    with pytest.raises(SnapshotError, match="not replaced"):
        manager.create("companion_a", "test-plugin", "1.0.0", "2.0.0")
    assert path.read_bytes() == b"fictional-invalid-key"
