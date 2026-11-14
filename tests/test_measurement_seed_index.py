from tests.measurement_tools import load

from clytie.backup.coordination import run_sync

fixture = load("measurement_fixture")


async def test_seed_only_lookup_index_is_gone_before_any_business(tmp_path, monkeypatch):
    from clytie.memory.store import MemoryStore

    original = MemoryStore._save
    observed = []

    def save(store, *args, **kwargs):
        names = {row[1] for row in store.db.execute("PRAGMA index_list(keywords)")}
        assert "measurement_seed_lookup" in names
        observed.append(names)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(MemoryStore, "_save", save)
    owner, metrics = await fixture.setup(
        tmp_path / "fixture", "small_text", "candidate", 73, tiny=True
    )
    try:
        assert len(observed) == 8
        names, free_pages = await run_sync(
            owner.store,
            lambda: (
                {row[0] for row in owner.store.db.execute("SELECT name FROM sqlite_master")},
                owner.store.db.execute("PRAGMA freelist_count").fetchone()[0],
            ),
        )
        assert "measurement_seed_lookup" not in names
        assert "benchmark_payload" not in names
        assert free_pages == 0
        await fixture.business(owner, 0)
        assert metrics["logical"]["records"] == metrics["logical"]["revisions"] == 8
    finally:
        await fixture.close(owner)
