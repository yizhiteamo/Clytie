import pytest
from tests.measurement_tools import load

construct = load("measurement_geometry").construct


async def test_geometry_preserves_canonical_scale_and_does_not_execute_workload(tmp_path):
    result = await construct(tmp_path / "geometry", ("small_text",))
    assert result["purpose"] == "geometry_only_not_performance"
    metrics = result["scales"]["small_text"]
    assert metrics["logical"]["records"] == metrics["logical"]["revisions"] == 128
    assert metrics["logical"]["file_bytes"] == 8 * 1024
    assert metrics["memory_database_bytes"] > metrics["logical"]["content_bytes"]
    assert not (tmp_path / "geometry/small_text/home/backups/regular").exists()
    with pytest.raises(FileExistsError):
        await construct(tmp_path / "geometry", ("small_text",))
