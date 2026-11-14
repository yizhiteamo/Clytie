"""重要变更的持久调度版本，不受派生维护和失败事务影响。"""

import pytest

from clytie.memory.store import MemoryStore
from clytie.sdk.testing import MockClock


def test_important_revision_commits_and_survives_reopen(tmp_path):
    path = tmp_path / "memory.sqlite3"
    clock = MockClock()
    store = MemoryStore(path, clock=clock, config={}, instance={})
    try:
        assert store.meta("backup_revision", 0) == 0
        record = store.write(
            {"kind": "fact", "content": "虚构岛有蓝色花", "visibility": "private"},
            scene="owner_private",
            conversation_id=None,
            operator="owner",
            owner="owner",
        )
        assert store.meta("backup_revision") == 1
        store.revise(
            record.record_id,
            record.revision,
            {"retracted": True},
            scene="owner_private",
            conversation_id=None,
            operator="owner",
        )
        assert store.meta("backup_revision") == 2
        with store.transaction():
            store.set_meta("derived_test_state", 1)
            store.audit("maintenance_test")
        assert store.meta("backup_revision") == 2
        with pytest.raises(RuntimeError, match="injected"):
            with store.transaction():
                store.mark_backup_dirty()
                raise RuntimeError("injected")
        assert store.meta("backup_revision") == 2
    finally:
        store.close()
    reopened = MemoryStore(path, clock=clock, config={}, instance={})
    try:
        assert reopened.meta("backup_revision") == 2
        with pytest.raises(RuntimeError, match="requires_transaction"):
            reopened.mark_backup_dirty()
    finally:
        reopened.close()
