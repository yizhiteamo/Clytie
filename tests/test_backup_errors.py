import threading

import pytest

from clytie.backup.coordination import CaptureError
from clytie.backup.errors import BackupError
from clytie.backup.journal import BackupJournal
from clytie.backup.restore import RestoreError
from clytie.backup.source import SourceError
from clytie.sdk.testing import MockClock


@pytest.mark.parametrize(
    "error,expected",
    [
        (CaptureError("capture_busy"), "capture_busy"),
        (SourceError("snapshot_unsupported"), "snapshot_unsupported"),
        (BackupError("space_insufficient"), "space_insufficient"),
        (RestoreError("restore_incompatible"), "restore_incompatible"),
        (OSError("fictional sensitive detail"), "worker_failed"),
        (RestoreError("fictional sensitive detail"), "worker_failed"),
    ],
)
def test_worker_result_keeps_fixed_reason_not_exception_text(
    tmp_path, monkeypatch, error, expected
):
    from clytie.backup import worker

    clock = MockClock()
    key = "a" * 32
    BackupJournal.accept(tmp_path, clock, key, "verify", {"backup_id": "fictional"})

    class Controls:
        received = True
        started = threading.Event()
        started.set()

        def __init__(self, *args, **kwargs):
            pass

        def check(self):
            pass

    def fail(*args):
        raise error

    monkeypatch.setattr(worker, "Controls", Controls)
    monkeypatch.setattr(worker, "dispatch", fail)
    result = worker.execute(tmp_path, key, stream=None, emit=lambda value: None, clock=clock)
    assert result["phase"] == "failed"
    assert result["reason"] == expected
    assert "fictional sensitive detail" not in str(result)
    assert BackupJournal.load(BackupJournal.path_for(tmp_path, key), clock).public() == result


def test_unrecognized_journal_reason_is_not_projected(tmp_path):
    journal, _ = BackupJournal.accept(tmp_path, MockClock(), "b" * 32, "verify", {})
    journal.phase("failed", reason="fictional sensitive detail")
    assert journal.public()["reason"] == "worker_failed"
