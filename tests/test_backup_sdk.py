import importlib.util
from datetime import datetime, timezone

import pytest


def backup_types():
    assert importlib.util.find_spec("clytie.sdk.backup") is not None
    from clytie.sdk import backup

    return backup


def test_backup_request_is_validated_frozen_and_serializable():
    b = backup_types()
    request = b.BackupRequest(
        operation_id="operation_a",
        companion_id="companion_a",
        plugin_id="plugin-a",
        data_version=1,
        deadline=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    assert b.BackupRequest.model_validate_json(request.model_dump_json()) == request
    with pytest.raises(ValueError):
        request.data_version = 2
    with pytest.raises(ValueError):
        request.model_copy(update={"data_version": 0})
    with pytest.raises(ValueError):
        request.model_copy(update={"deadline": datetime(2030, 1, 1)})


def test_opaque_handles_are_preserved_and_never_serialized():
    b = backup_types()
    handle = object()
    ticket = b.BackupTicket(handle=handle)
    view = b.BackupView(epoch="epoch_a", data_version=1, revision="revision_a", handle=handle)
    assert ticket.handle is handle
    assert view.handle is handle
    assert "handle" not in repr(view)
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(ticket)
    with pytest.raises(TypeError):
        import pickle

        pickle.dumps(view)


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "C:/absolute", "a\\b", "a/../b", "", "a//b"]
)
def test_backup_entry_rejects_unsafe_paths(path):
    b = backup_types()
    with pytest.raises(ValueError):
        b.BackupEntry(relative_path=path, size=1, sha256="a" * 64)


def test_backup_entry_validates_size_and_digest():
    b = backup_types()
    entry = b.BackupEntry(relative_path="state/data.db", size=0, sha256="a" * 64)
    assert b.BackupEntry.model_validate_json(entry.model_dump_json()) == entry
    with pytest.raises(ValueError):
        entry.model_copy(update={"size": -1})
    with pytest.raises(ValueError):
        entry.model_copy(update={"sha256": "invalid"})


def test_backup_assets_are_validated_and_deeply_snapshotted():
    from clytie.sdk import BackupView
    from clytie.sdk.types import AssetRef

    ref = AssetRef(asset_id="a" * 64, media_type="text/plain", size=3, origin="fictional")
    original = [ref]
    view = BackupView("e", 1, "r", object(), original)
    original.clear()
    object.__setattr__(ref, "size", 20)
    assert len(view.assets) == 1
    assert view.assets[0].size == 3
    with pytest.raises(ValueError):
        view.assets[0].size = 10
    assert BackupView("e", 1, "r", object()).assets == ()
    with pytest.raises(ValueError):
        BackupView("e", 1, "r", object(), ({"asset_id": "a" * 64},))
    object.__setattr__(ref, "asset_id", "invalid")
    with pytest.raises(ValueError):
        BackupView("e", 1, "r", object(), (ref,))


async def test_reusable_provider_contract_checks_epoch_and_releases_on_failure():
    assert importlib.util.find_spec("clytie.sdk.testing.backup_contracts") is not None
    from clytie.sdk.testing.backup_contracts import assert_backup_provider_contract
    from clytie.sdk.testing.clock import MockClock

    b = backup_types()

    class Provider:
        def __init__(self, epoch):
            self.epoch = epoch
            self.releases = 0

        async def prepare(self, request, cancel):
            return b.BackupTicket(handle=object())

        async def capture(self, ticket, epoch, cancel):
            return b.BackupView(
                epoch=self.epoch or epoch,
                data_version=1,
                revision="revision_a",
                handle=ticket.handle,
            )

        async def materialize(self, view, sink, cancel):
            return None

        async def release(self, ticket):
            self.releases += 1

    class Sink:
        async def write(self, relative_path, chunks):
            raise AssertionError("empty fixture has no files")

    request = b.BackupRequest(
        operation_id="operation_a",
        companion_id="companion_a",
        plugin_id="plugin-a",
        data_version=1,
        deadline=MockClock().now(),
    )
    good = Provider(None)
    await assert_backup_provider_contract(good, request, Sink())
    assert good.releases == 2
    bad = Provider("wrong_epoch")
    with pytest.raises(AssertionError, match="epoch"):
        await assert_backup_provider_contract(bad, request, Sink())
    assert bad.releases == 2


def test_backup_contracts_exported_at_sdk_boundary():
    import clytie.sdk as sdk

    b = backup_types()
    for name in [
        "BackupRequest",
        "BackupTicket",
        "BackupView",
        "BackupEntry",
        "BackupProvider",
        "BackupSink",
    ]:
        assert getattr(sdk, name, None) is getattr(b, name)
