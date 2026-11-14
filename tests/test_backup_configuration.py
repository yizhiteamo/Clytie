import pytest
from tests.test_config import make_home, write_yaml

from clytie.sdk.testing import MockClock


def writer(tmp_path):
    import importlib.util

    assert importlib.util.find_spec("clytie.backup.configuration") is not None
    from clytie.backup.configuration import BackupConfigWriter

    home, repository, instance, _ = make_home(tmp_path)
    return BackupConfigWriter(home, MockClock(), repo_root=repository), home, instance


def test_backup_plan_preserves_unrelated_bytes_and_requires_confirmation(tmp_path):
    w, home, instance = writer(tmp_path)
    path = home / "clytie.local.yaml"
    before = path.read_bytes() + b"# untouched tail\n"
    path.write_bytes(before)
    plan = w.plan({"enabled": False})
    assert plan["requires_confirmation"] is True
    assert path.read_bytes() == before
    with pytest.raises(ValueError, match="confirmation_required"):
        w.apply(plan["plan_id"])
    result = w.apply(plan["plan_id"], confirmed=True)
    assert result["saved"]
    assert path.read_bytes().startswith(before)
    assert list((home / "config-history").iterdir())[0].read_bytes() == before


def test_enabled_mirror_configuration_requires_repository_acceptance(tmp_path, monkeypatch):
    from clytie.backup.mirror import MirrorManager, StorageIdentity
    from clytie.backup.storage import BackupStorage

    w, home, _ = writer(tmp_path)
    storage = BackupStorage(home, w.clock)
    destination = tmp_path / "explicit_mirror"
    destination.mkdir()

    def probe(path):
        path = path.resolve()
        device = "primary" if path == storage.root else "fictional_second_disk"
        return StorageIdentity(str(path), str(path.stat().st_ino), "volume-" + device, (device,))

    manager = MirrorManager(
        home / "supervisor/backup-mirror",
        storage.root,
        w.clock,
        owner_id=storage.instance_id(),
        forbidden_roots=(home,),
        probe=probe,
    )
    monkeypatch.setattr(w, "_mirror_manager", lambda: manager)
    plan = w.plan({"mirror": {"enabled": True, "path": str(destination)}})
    assert "mirror_repository_acceptance" in plan["confirmation_reasons"]
    assert not list(destination.iterdir())
    with pytest.raises(ValueError, match="confirmation_required"):
        w.apply(plan["plan_id"])
    assert w.apply(plan["plan_id"], confirmed=True)["saved"]
    assert manager.status()["status"] == "switch_pending"
    assert (
        manager.state()["active"] is None
    )  # Configuration acceptance is not verified copy success.


def test_backup_patch_preserves_comments_crlf_and_scalar_bytes(tmp_path):
    w, home, _ = writer(tmp_path)
    path = home / "clytie.local.yaml"
    original = (
        path.read_bytes().replace(b"\n", b"\r\n")
        + b"backup: # backup note\r\n  retention:\r\n    target_gib: 20 # budget note\r\n"
    )
    path.write_bytes(original)
    plan = w.plan({"retention": {"target_gib": 3}})
    assert plan["impact"]["cleanup"] == "next_successful_automatic_backup"
    assert plan["impact"]["target_is_hard_limit"] is False
    w.apply(plan["plan_id"], confirmed=True)
    assert path.read_bytes() == original.replace(b"target_gib: 20", b"target_gib: 3")


def test_two_plans_and_external_change_are_stale(tmp_path):
    w, home, _ = writer(tmp_path)
    one = w.plan({"logs": {"archives": 8}})
    two = w.plan({"logs": {"archives": 7}})
    w.apply(one["plan_id"])
    with pytest.raises(ValueError, match="plan_stale"):
        w.apply(two["plan_id"])


def test_plan_uses_strict_full_configuration_validation(tmp_path):
    w, home, instance = writer(tmp_path)
    instance["accounts"] = {"account_a": {"channel": "fake", "account": "fictional"}}
    instance["accounts"]["account_a"]["secret"] = "missing.key"
    write_yaml(home / "clytie.local.yaml", instance)
    with pytest.raises(ValueError, match="config_invalid"):
        w.plan({"enabled": False})


def test_invalid_backup_patch_and_postwrite_validation_restore(tmp_path):
    w, home, _ = writer(tmp_path)
    path = home / "clytie.local.yaml"
    before = path.read_bytes()
    with pytest.raises(ValueError, match="config_invalid"):
        w.plan({"retention": {"minimum_per_group": 2}})
    plan = w.plan({"enabled": False})
    w.validate = lambda: False
    with pytest.raises(ValueError, match="config_invalid_after_write"):
        w.apply(plan["plan_id"], confirmed=True)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "patch",
    [
        {"memory": {"enabled": False}},
        {"retention": {"daily_days": 1}},
        {"retention": {"monthly_months": 12}},
        {"mirror": {"path": "separate-backup"}},
    ],
)
def test_sensitive_plan_confirmation_is_computed_server_side(tmp_path, patch):
    w, home, _ = writer(tmp_path)
    plan = w.plan(patch)
    assert plan["requires_confirmation"]
    with pytest.raises(ValueError, match="confirmation_required"):
        w.apply(plan["plan_id"])


def test_history_keeps_ten_explicit_sources_and_preserves_unknown_files(tmp_path):
    w, home, _ = writer(tmp_path)
    history = home / "config-history"
    history.mkdir()
    foreign = history / "do-not-remove.txt"
    foreign.write_text("fictional retained data")
    for count in range(11, 23):
        plan = w.plan({"logs": {"archives": count}})
        w.apply(plan["plan_id"])
        w.clock.advance(1)
    assert len(list(history.iterdir())) == 11
    assert foreign.read_text() == "fictional retained data"


def test_two_concurrent_plans_have_only_one_commit(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from clytie.plugin_host.locking import OperationBusy

    w, home, _ = writer(tmp_path)
    plans = [w.plan({"logs": {"archives": count}}) for count in [7, 8]]
    barrier = Barrier(2)

    def run(plan):
        barrier.wait()
        try:
            return w.apply(plan["plan_id"])["saved"]
        except (OperationBusy, ValueError):
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, plans)) == [False, True]


def test_candidate_validation_does_not_hold_commit_gate(tmp_path):
    from clytie.backup.configuration import config_commit_lock

    w, home, _ = writer(tmp_path)
    report = w._report

    def checked(source):
        with config_commit_lock(home):
            pass
        return report(source)

    w._report = checked
    plan = w.plan({"logs": {"archives": 8}})
    w.apply(plan["plan_id"])


def test_gate_prevents_commit_and_unsafe_yaml_refused(tmp_path):
    from clytie.backup.configuration import config_commit_lock
    from clytie.plugin_host.locking import OperationBusy

    w, home, _ = writer(tmp_path)
    path = home / "clytie.local.yaml"
    before = path.read_bytes()
    plan = w.plan({"logs": {"archives": 8}})
    with config_commit_lock(home):
        with pytest.raises(OperationBusy):
            w.apply(plan["plan_id"])
    assert path.read_bytes() == before
    path.write_bytes(before + b"backup: {enabled: true}\n")
    with pytest.raises(ValueError, match="config_not_writable"):
        w.plan({"enabled": False})


def test_legacy_writer_and_backup_writer_share_capture_commit_gate(tmp_path):
    from clytie.api.auth import APIError
    from clytie.api.config_write import ConfigWriter
    from clytie.backup.configuration import config_commit_lock
    from clytie.plugin_host.locking import OperationBusy

    w, home, _ = writer(tmp_path)
    legacy = ConfigWriter(home, w.clock, validate=lambda: True)
    companion = home / "companions/companion_a/companion.yaml"
    before = companion.read_bytes()
    plan = w.plan({"logs": {"archives": 8}})
    with config_commit_lock(home):
        with pytest.raises(APIError, match="config_not_writable"):
            legacy.save("companion_a", {"plugins": {"fake-model": {"enabled": True}}})
        with pytest.raises(OperationBusy):
            w.apply(plan["plan_id"])
    assert companion.read_bytes() == before


def test_legacy_validation_is_outside_gate_and_rollback_reacquires_it(tmp_path, monkeypatch):
    from contextlib import contextmanager

    from clytie.api.config_write import ConfigWriter
    from clytie.backup import configuration

    w, home, _ = writer(tmp_path)
    original = configuration.config_commit_lock
    original_commit = ConfigWriter._commit
    entries = []
    active = []

    @contextmanager
    def observed(self):
        with original_commit(self):
            entries.append("commit")
            active.append(True)
            try:
                yield
            finally:
                active.pop()

    monkeypatch.setattr(ConfigWriter, "_commit", observed)

    def invalid():
        assert not active
        with original(home):
            pass
        return False

    legacy = ConfigWriter(home, w.clock, validate=invalid)
    companion = home / "companions/companion_a/companion.yaml"
    before = companion.read_bytes()
    from clytie.api.auth import APIError

    with pytest.raises(APIError, match="config_invalid_after_write"):
        legacy.save("companion_a", {"plugins": {"fake-model": {"enabled": True}}})
    assert entries == ["commit", "commit"]
    assert companion.read_bytes() == before
