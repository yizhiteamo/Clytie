import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def benchmark():
    path = Path(__file__).resolve().parents[1] / "scripts/benchmark_backup.py"
    spec = importlib.util.spec_from_file_location("backup_benchmark", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_percentiles_preserve_tail_and_empty_is_explicit():
    module = benchmark()
    assert module.percentiles([1, 2, 3, 4, 100]) == {
        "count": 5,
        "p50": 3,
        "p95": 100,
        "p99": 100,
        "max": 100,
    }
    assert module.percentiles([]) is None


def adverse_result():
    return {
        "scale": "many_small_files",
        "scale_parameters": (128, 2048, 1024),
        "with_backup": True,
        "requests": 800,
        "interval_seconds": 0.025,
        "response_ms": {"count": 800, "p50": 1, "p95": 20, "p99": 100, "max": 500},
        "write_wait_ms": None,
        "samples_during_backup": 796,
        "overlap_response_ms": None,
        "backup": {
            "seconds": 98 + 7144 / 10**4,
            "completeness": "partial",
            "missing": [
                {"component": "plugin:companion_a:fictional", "reason": "provider_timeout"}
            ],
            "package_bytes": 128 * 1024**2,
            "verified_files": 2,
        },
        "peak_rss_bytes": 64 * 1024**2,
        "peak_temporary_bytes": 256 * 1024**2,
        "peak_wal_bytes": 1024**2,
        "errors": [{"index": 10, "type": "CaptureError"}],
        "capture_gate_hold_ms": {"count": 1, "p50": 251, "p95": 251, "p99": 251, "max": 251},
        "capture_gate_wait_ms": None,
        "capture_hold_exceeded_250ms": True,
        "capture_wait_exceeded_2000ms": True,
        "backup_load_coverage_ratio": 0.2,
        "backup_load_coverage_seconds": 19 + 7429 / 10**4,
    }


def test_publishable_summary_preserves_failures_limits_and_precision():
    text = benchmark().render_summary(
        {"seed": 0x11DA9, "script_sha256": "a" * 64, "background_load": "concurrent tests"},
        [adverse_result()],
    )
    for required in (
        "partial",
        "provider_timeout",
        "CaptureError",
        "true",
        "20.000%",
        "98.714",
        "251.000",
        "concurrent tests",
    ):
        assert required in text
    assert "128.000 MiB" in text
    assert str(128 * 1024**2) not in text
    assert str(0x11DA9) not in text
    assert "0x11da9" in text
    assert str(adverse_result()["backup"]["seconds"]) not in text
    assert "未测量" in text
    assert "a" * 64 in text


def test_display_rounding_does_not_hide_incomplete_coverage_or_gate_breach():
    result = adverse_result()
    result["backup_load_coverage_ratio"] = 1 - 1 / 10**7
    result["capture_gate_hold_ms"] = {
        "count": 1,
        **{key: 250 + 1 / 10**4 for key in ("p50", "p95", "p99", "max")},
    }
    text = benchmark().render_summary({}, [result])
    assert "100.000%" in text
    assert "负载未覆盖完整备份（按未舍入值）：true" in text
    assert "250.000" in text
    assert "捕获持有超过 250 ms：true" in text


def test_large_row_count_uses_exact_thousand_row_unit():
    result = adverse_result()
    result["scale_parameters"] = (32 * 1024, 8, 1024)
    text = benchmark().render_summary({}, [result])
    assert "32.768 千行" in text
    assert str(32 * 1024) not in text


@pytest.mark.asyncio
async def test_main_stdout_does_not_publish_raw_metrics(tmp_path, monkeypatch, capsys):
    module = benchmark()

    async def fake_case(*args, **kwargs):
        return adverse_result()

    monkeypatch.setattr(module, "run_case", fake_case)
    output = tmp_path / "summary"
    await module.main(
        SimpleNamespace(
            output=output,
            conditions_note="unit test",
            scales=["many_small_files"],
            requests=800,
            interval=0.025,
        )
    )
    assert str(128 * 1024**2) not in capsys.readouterr().out
    raw = json.loads((output / "summary.json").read_text())
    assert raw[0]["backup"]["package_bytes"] == 128 * 1024**2
    assert "provider_timeout" in (output / "summary.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_benchmark_real_online_capture_and_fixed_load(tmp_path):
    module = benchmark()
    result = await module.run_case(
        tmp_path / "case",
        "small_text",
        backup=True,
        requests=30,
        interval=0.01,
        scale_override=(16, 2, 128),
    )
    assert result["backup"]["completeness"] == "complete"
    assert result["backup"]["verified_files"] > 2
    assert result["response_ms"]["count"] == 30
    assert result["write_wait_ms"]["count"] == 30
    assert result["samples_during_backup"] > 0
    assert result["peak_rss_bytes"] > 0
    assert result["peak_temporary_bytes"] >= result["backup"]["package_bytes"]
    assert result["errors"] == []
    assert result["capture_gate_hold_ms"]["count"] == 1
    assert result["capture_gate_wait_ms"]["count"] == 1
    assert all(sample["response_ms"] >= 0 for sample in result["samples"])
    assert all(sample["start_s"] >= sample["scheduled_s"] for sample in result["samples"])


@pytest.mark.asyncio
async def test_large_asset_benchmark_uses_production_registry_and_declaration(tmp_path):
    module = benchmark()
    result = await module.run_case(
        tmp_path / "case",
        "large_asset",
        backup=True,
        requests=30,
        interval=0.01,
        scale_override=(16, 1, 128),
    )
    assert result["backup"]["completeness"] == "complete"
    assert result["backup"]["verified_assets"] == 1
    assert result["errors"] == []
    registry = tmp_path / "case/work/verified/shared/assets/registry.json"
    assert json.loads(registry.read_bytes())["registrations"][0]["companion_id"] == "companion_a"
