import pytest

from clytie.backup.keys import BackupKeys, KeyErrorCode
from clytie.sdk.testing import MockClock


def test_key_custody_declaration_is_not_verified_copy(tmp_path):
    home = tmp_path / "home"
    service = BackupKeys(home, MockClock(), repository=tmp_path / "repository")
    service.ensure()
    initial = service.status()
    assert initial["needs_safekeeping"] and not initial["copy_verified"]
    service.acknowledge()
    assert service.status()["acknowledged"]
    assert not service.status()["copy_verified"]
    destination = tmp_path / "saved-key"
    exported = service.export(destination)
    assert set(exported) == {"fingerprint", "exported"}
    assert not service.status()["copy_verified"]
    assert service.verify(destination)["verified"]
    assert service.status()["copy_verified"]
    with pytest.raises(KeyErrorCode):
        service.export(destination)


@pytest.mark.parametrize("relative", ["home/backups/regular/key", "repository/key", "mirror/key"])
def test_key_export_refuses_backup_or_public_repository(tmp_path, relative):
    service = BackupKeys(
        tmp_path / "home",
        MockClock(),
        repository=tmp_path / "repository",
        mirror=tmp_path / "mirror",
    )
    service.ensure()
    target = tmp_path / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(KeyErrorCode):
        service.export(target)
    assert not target.exists()


def test_missing_key_with_old_products_never_creates_replacement(tmp_path):
    home = tmp_path / "home"
    product = home / "backups/regular/fictional.clytie-backup"
    product.parent.mkdir(parents=True)
    product.write_bytes(b"fictional-old-encrypted-product")
    service = BackupKeys(home, MockClock(), repository=tmp_path / "repository")
    with pytest.raises(KeyErrorCode, match="key_missing"):
        service.ensure()
    assert not (home / "secrets/backup.key").exists()
    assert product.read_bytes() == b"fictional-old-encrypted-product"


def test_wrong_key_copy_does_not_override_verified_evidence(tmp_path):
    service = BackupKeys(tmp_path / "home", MockClock(), repository=tmp_path / "repository")
    service.ensure()
    target = tmp_path / "copy"
    service.export(target)
    service.verify(target)
    target.write_bytes(b"x" * 32)
    with pytest.raises(KeyErrorCode, match="key_mismatch"):
        service.verify(target)
    assert not service.status()["copy_verified"]
    assert "key" not in service.status()


def test_empty_initialized_repository_does_not_require_existing_key(tmp_path):
    from clytie.backup.storage import BackupStorage

    home = tmp_path / "home"
    clock = MockClock()
    BackupStorage(home, clock)
    service = BackupKeys(home, clock, repository=tmp_path / "repository")
    service.ensure()
    assert service.status()["available"]


def test_historical_key_receipt_without_key_never_generates_replacement(tmp_path):
    service = BackupKeys(tmp_path / "home", MockClock(), repository=tmp_path / "repository")
    service.ensure()
    service.path.unlink()
    with pytest.raises(KeyErrorCode, match="key_missing"):
        service.ensure()
    assert not service.path.exists()
