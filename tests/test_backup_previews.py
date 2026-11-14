import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest

from clytie.sdk.testing.clock import MockClock


def test_preview_expiry_and_independent_reader(tmp_path):
    from clytie.backup.previews import PreviewCatalog, PreviewError

    clock = MockClock()
    catalog = PreviewCatalog(tmp_path, clock)
    source = tmp_path / "source"
    source.write_bytes(b"fictional backup")
    catalog.reference(source, "generation", kind="generation")
    plan = catalog.publish(
        "instance", "a" * 32, source, "generation", target="target", fingerprint="hash"
    )
    assert plan["state"] == "ready"
    with catalog.reading("instance", plan["plan_id"]):
        clock.advance(24 * 60 * 60)
        assert catalog.get("instance", plan["plan_id"])["state"] == "expired"
        with catalog.deletion(source) as allowed:
            assert not allowed
        with pytest.raises(PreviewError, match="plan_expired"):
            catalog.accept("instance", plan["plan_id"], "b" * 32)
    with catalog.deletion(source) as allowed:
        assert allowed
    clock._now -= timedelta(days=2)
    assert catalog.get("instance", plan["plan_id"])["state"] == "expired"


def test_consumed_is_single_operation_and_cancel_does_not_release(tmp_path):
    from clytie.backup.previews import PreviewCatalog, PreviewError

    catalog = PreviewCatalog(tmp_path, MockClock())
    source = tmp_path / "source"
    source.touch()
    catalog.reference(source, "generation", kind="generation")
    catalog.publish(
        "memory", "a" * 32, source, "generation", target="companion_a", fingerprint="hash"
    )
    assert catalog.accept("memory", "a" * 32, "b" * 32) == ("b" * 32, True)
    assert catalog.accept("memory", "a" * 32, "c" * 32) == ("b" * 32, False)
    bindings = catalog.for_operation("b" * 32)
    assert bindings[0]["target"] == "companion_a" and bindings[0]["kind"] == "memory"
    bindings[0]["history"].clear()
    assert catalog.for_operation("b" * 32)[0]["history"]
    with pytest.raises(PreviewError, match="plan_in_use") as error:
        catalog.cancel("memory", "a" * 32)
    assert error.value.operation_id == "b" * 32
    with catalog.deletion(source) as allowed:
        assert not allowed


def test_cancel_and_stale_are_terminal_and_type_is_private(tmp_path):
    from clytie.backup.previews import PreviewCatalog, PreviewError

    catalog = PreviewCatalog(tmp_path, MockClock())
    source = tmp_path / "source"
    source.touch()
    catalog.reference(source, "generation", kind="generation")
    catalog.publish(
        "memory", "a" * 32, source, "generation", target="companion_a", fingerprint="hash"
    )
    with pytest.raises(PreviewError, match="plan_not_found"):
        catalog.get("instance", "a" * 32)
    assert catalog.cancel("memory", "a" * 32)["state"] == "cancelled"
    assert catalog.cancel("memory", "a" * 32)["state"] == "cancelled"
    with pytest.raises(PreviewError, match="plan_cancelled"):
        with catalog.reading("memory", "a" * 32):
            pytest.fail("cancelled preview was readable")


CHILD = """
import json,sys
from pathlib import Path
from clytie.backup.previews import PreviewCatalog, PreviewError
from clytie.sdk.testing.clock import MockClock
home, action = Path(sys.argv[1]), sys.argv[2]
clock = MockClock()
catalog = PreviewCatalog(home, clock)
print('ready', flush=True)
sys.stdin.readline()
if action.startswith('apply'):
    try:
        result = catalog.accept('instance', 'a'*32, ('c' if action == 'apply_second' else 'b')*32)
        print(json.dumps({'accepted':result[0]}), flush=True)
    except PreviewError as e:
        print(json.dumps({'error':e.code}), flush=True)
elif action == 'expire':
    clock.advance(24 * 60 * 60)
    with catalog.deletion(home/'source') as allowed:
        if allowed:
            (home/'source').unlink()
        print(json.dumps({'deleted':allowed}), flush=True)
elif action == 'orphan':
    with catalog.reading('instance', 'a'*32):
        print('reading', flush=True)
        sys.stdin.readline()
"""


def _published(tmp_path):
    from clytie.backup.previews import PreviewCatalog

    catalog = PreviewCatalog(tmp_path, MockClock())
    source = tmp_path / "source"
    source.write_bytes(b"fictional backup")
    catalog.reference(source, "generation", kind="generation")
    catalog.publish("instance", "a" * 32, source, "generation", target="target", fingerprint="hash")
    return catalog, source


def _child(tmp_path, action):
    child = subprocess.Popen(
        [sys.executable, "-c", CHILD, str(tmp_path), action],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert child.stdout.readline().strip() == "ready"
    return child


def test_real_process_apply_expire_and_rotation_race(tmp_path):
    catalog, source = _published(tmp_path)
    children = [_child(tmp_path, action) for action in ("apply", "expire")]
    try:
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in children:
            output, error = child.communicate(timeout=20)
            assert child.returncode == 0, error
            results.append(json.loads(output))
        applied, expired = results
        if "accepted" in applied:
            assert not expired["deleted"] and source.exists()
            assert catalog.get("instance", "a" * 32)["state"] == "consumed"
        else:
            assert applied["error"] == "plan_expired"
            assert expired["deleted"] and not source.exists()
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def test_killed_reader_rebuild_keeps_source_protected(tmp_path):
    catalog, source = _published(tmp_path)
    child = _child(tmp_path, "orphan")
    try:
        child.stdin.write("go\n")
        child.stdin.flush()
        assert child.stdout.readline().strip() == "reading"
    finally:
        child.kill()
        child.wait(timeout=10)
    catalog.clock.advance(24 * 60 * 60)
    assert catalog.get("instance", "a" * 32)["state"] == "expired"
    from clytie.backup.previews import PreviewCatalog

    rebuilt = PreviewCatalog(tmp_path, catalog.clock)
    with rebuilt.deletion(source) as allowed:
        assert not allowed
    holders = rebuilt.holders(source)
    assert len(holders) == 1 and holders[0]["kind"] == "reader"
    assert holders[0]["executor"]["started_at"]


def test_publication_time_no_renewal_and_stale_before_expiry(tmp_path):
    from clytie.backup.previews import PreviewCatalog, PreviewError

    clock = MockClock()
    catalog = PreviewCatalog(tmp_path, clock)
    source = tmp_path / "source"
    source.touch()
    catalog.reference(source, "generation", kind="generation")
    clock.advance(3600)
    plan = catalog.publish(
        "instance", "a" * 32, source, "generation", target="target", fingerprint="hash"
    )
    assert plan["created_at"] == clock.now().isoformat()
    clock.advance(24 * 60 * 60 - 1)
    duplicate = catalog.publish(
        "instance", "a" * 32, source, "generation", target="target", fingerprint="hash"
    )
    assert duplicate == plan
    assert catalog.get("instance", "a" * 32)["state"] == "ready"

    def changed():
        raise ValueError("restore_source_changed")

    with pytest.raises(ValueError, match="restore_source_changed"):
        catalog.accept("instance", "a" * 32, "b" * 32, validate=changed)
    assert catalog.get("instance", "a" * 32)["state"] == "stale"
    with pytest.raises(PreviewError, match="plan_stale"):
        catalog.accept("instance", "a" * 32, "c" * 32)
    assert catalog.get("instance", "a" * 32)["history"][-1]["state"] == "stale"


def test_real_process_double_apply_uses_original_operation(tmp_path):
    catalog, source = _published(tmp_path)
    children = [_child(tmp_path, action) for action in ("apply", "apply_second")]
    try:
        for child in children:
            child.stdin.write("go\n")
            child.stdin.flush()
        results = []
        for child in children:
            output, error = child.communicate(timeout=20)
            assert child.returncode == 0, error
            results.append(json.loads(output))
        assert results[0]["accepted"] == results[1]["accepted"]
        holders = catalog.holders(source)
        assert len(holders) == 1 and holders[0]["kind"] == "execution"
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


def test_missing_ready_reference_fails_closed(tmp_path):
    from clytie.backup.previews import PreviewError

    catalog, source = _published(tmp_path)
    data = json.loads(catalog.path.read_bytes())
    data["references"] = {}
    catalog.path.write_text(json.dumps(data))
    with pytest.raises(PreviewError, match="reference_recovery_blocked"):
        with catalog.deletion(source):
            pytest.fail("corrupt references must never permit deletion")


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing violation contract")
def test_catalog_and_operation_commit_retry_only_atomic_replace(tmp_path, monkeypatch):
    from pathlib import Path

    from clytie.backup.journal import BackupJournal

    catalog, source = _published(tmp_path)
    journal, _ = BackupJournal.accept(tmp_path, MockClock(), "c" * 32, "verify", {})
    replace = Path.replace
    remaining = {catalog.path: 2, journal.path: 2}

    def sharing(path, target):
        if remaining.get(target, 0):
            remaining[target] -= 1
            error = PermissionError("fictional sharing conflict")
            error.winerror = 5
            raise error
        return replace(path, target)

    monkeypatch.setattr(Path, "replace", sharing)
    catalog.accept("instance", "a" * 32, "b" * 32)
    journal.commit({"verification": "passed"})
    assert remaining == {catalog.path: 0, journal.path: 0}
    assert catalog.get("instance", "a" * 32)["operation_id"] == "b" * 32
    assert BackupJournal.load(journal.path, MockClock()).value["phase"] == "committed"
    assert len(catalog.holders(source)) == 1
