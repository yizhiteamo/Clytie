"""The existing download ceiling can only be tightened by a caller."""

import io

import pytest
from tests.measurement_tools import load

proposed = load("measurement_transport")


def fake_process(monkeypatch, raw):
    class Process:
        stdout = io.BytesIO(raw)

        def wait(self):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(proposed.subprocess, "Popen", lambda *args, **kwargs: Process())


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "10"])
def test_invalid_bound_rejected_before_creating_file(tmp_path, limit):
    with pytest.raises(proposed.evidence.EvidenceError):
        proposed._download_archive("fictional", tmp_path / "archive", max_bytes=limit)
    assert not (tmp_path / "archive").exists()


def test_tight_bound_rejects_and_preserves_partial(tmp_path, monkeypatch):
    fake_process(monkeypatch, b"too large")
    with pytest.raises(proposed.evidence.EvidenceError):
        proposed._download_archive("fictional", tmp_path / "archive", max_bytes=3)
    assert (tmp_path / "archive").exists()


def test_old_default_bound_still_accepts(tmp_path, monkeypatch):
    monkeypatch.setattr(proposed.evidence, "MAX_TOTAL", 4)
    raw = b"x" * (4 + 2 * 1024 * 1024)
    fake_process(monkeypatch, raw)
    proposed._download_archive("fictional", tmp_path / "archive")
    assert (tmp_path / "archive").read_bytes() == raw


def test_old_default_bound_still_rejects(tmp_path, monkeypatch):
    monkeypatch.setattr(proposed.evidence, "MAX_TOTAL", 4)
    fake_process(monkeypatch, b"x" * (5 + 2 * 1024 * 1024))
    with pytest.raises(proposed.evidence.EvidenceError):
        proposed._download_archive("fictional", tmp_path / "archive")


def test_cannot_relax_old_bound(tmp_path):
    with pytest.raises(proposed.evidence.EvidenceError):
        proposed._download_archive(
            "fictional",
            tmp_path / "archive",
            max_bytes=proposed.evidence.MAX_TOTAL + 2 * 1024 * 1024 + 1,
        )
