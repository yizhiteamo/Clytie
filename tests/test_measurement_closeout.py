"""Offline fixed-scene binding checks; never spawn a trial or contact GitHub."""

import copy
from pathlib import Path

import pytest
from tests.measurement_tools import load

closeout = load("measurement_closeout")


@pytest.fixture
def material(tmp_path, monkeypatch):
    frozen = closeout.ingress.unpack(
        (closeout.ROOT / ".github/a43-mechanism-plan.json").read_bytes()
    )
    runtime = dict(
        environment=dict(
            cpu_count=4, os="Windows", release_sha256="a" * 64, machine_sha256="b" * 64
        ),
        python="3.10.11",
        sqlite="3.40.1",
    )
    for system in ("Windows", "Linux"):
        item = copy.deepcopy(runtime)
        item["environment"]["os"] = system
        frozen["plan"]["inputs"]["inventory"]["environments"][system] = closeout.plan.digest(item)
    paths = (
        ".github/a43-mechanism-plan.json",
        "scripts/measurement_closeout.py",
        ".github/workflows/closeout-windows.yml",
        ".github/workflows/closeout-linux.yml",
    )
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            closeout.ingress.pack(frozen) if name.endswith("plan.json") else b"fixture"
        )
    monkeypatch.setattr(closeout, "ROOT", tmp_path)
    monkeypatch.setattr(
        closeout.launch, "tool_inventory", lambda root: {"files": {}, "sha256": "a" * 64}
    )
    env = dict(
        GITHUB_REPOSITORY="yizhiteamo/Clytie",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_RUN_ID="123",
        GITHUB_JOB="closeout_windows",
    )
    request = dict(format=1, scene=0, product=closeout.PRODUCT, tool="b" * 40, prior_verified=[])
    return request, runtime, env


@pytest.mark.parametrize("scene", range(4))
def test_exact_four_scenes_preserve_fixture_and_post_start_budget(material, scene):
    request, runtime, env = material
    request.update(scene=scene, prior_verified=list(range(scene)))
    runtime["environment"]["os"] = closeout.SCENES[scene][0]
    run = closeout.checked(request, runtime, env)
    assert run["phase"] == "closeout" and run["scenario"] == "normal"
    assert run["load"]["backup_start_seconds"] == 120
    assert run["load"]["interval_seconds"] == 0.025
    assert run["execution"]["order"] == ["candidate"]
    assert run["source_commit"] == closeout.PRODUCT
    closeout.evidence.validate_run(run)


@pytest.mark.parametrize("change", ["retry", "scene", "product", "runtime", "unknown", "prior"])
def test_changed_identity_or_extra_scope_rejected(material, change):
    request, runtime, env = material
    if change == "retry":
        env["GITHUB_RUN_ATTEMPT"] = "2"
    if change == "scene":
        request["scene"] = 4
    if change == "product":
        request["product"] = "c" * 40
    if change == "runtime":
        runtime["environment"]["cpu_count"] = 8
    if change == "unknown":
        request["command"] = "never_execute"
    if change == "prior":
        request["prior_verified"] = [0]
    with pytest.raises(ValueError):
        closeout.checked(request, runtime, env)


def test_workflow_uploads_only_encrypted_outputs_and_never_retries():
    root = Path(__file__).resolve().parents[1]
    for platform in ("windows", "linux"):
        text = (root / f".github/workflows/closeout-{platform}.yml").read_text(encoding="utf-8")
        assert "workflow_dispatch" not in text
        assert "GITHUB_RUN_ATTEMPT']=='1'" in text
        assert "steps.scene.outputs.ciphertext" in text
        assert "steps.scene.outputs.receipt" in text
        assert "retention-days: 14" in text
        assert "continue-on-error" not in text


@pytest.mark.parametrize("seal_failed", [False, True])
def test_entry_exports_paths_only_after_seal(material, tmp_path, monkeypatch, seal_failed):
    import json
    import sys
    from types import SimpleNamespace

    request, runtime, env = material
    base = tmp_path / "runner"
    base.mkdir()
    (base / "closeout-request.json").write_bytes(closeout.ingress.pack(request))
    event = tmp_path / "event.json"
    event.write_text(
        json.dumps(
            dict(
                action="synchronize",
                number=71,
                pull_request=dict(head=dict(repo=dict(full_name="yizhiteamo/Clytie"))),
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "outputs"
    env.update(RUNNER_TEMP=str(base), GITHUB_EVENT_PATH=str(event), GITHUB_OUTPUT=str(output))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    public_source = Path(__file__).resolve().parents[1] / ".github/a43-preparation-public.json"
    (tmp_path / ".github/a43-preparation-public.json").write_bytes(public_source.read_bytes())
    monkeypatch.setattr(
        closeout.subprocess, "check_output", lambda *a, **k: request["tool"].encode()
    )
    monkeypatch.setattr(closeout.preparation, "runtime", lambda: runtime)
    monkeypatch.setattr(closeout.versions, "prepare", lambda *a, **k: None)
    monkeypatch.setattr(closeout.versions, "activate", lambda *a: (tmp_path, {}))

    async def fake_trial(directory, manifest, source, *, tiny):
        assert tiny is False
        raw = directory / "raw"
        raw.mkdir(parents=True)
        (raw / "run-manifest.json").write_text(json.dumps({**manifest, "result": "fail"}))

    monkeypatch.setitem(sys.modules, "measurement_trial", SimpleNamespace(_execute=fake_trial))

    def seal(source, target, recipient, expected):
        if seal_failed:
            raise ValueError("fictional_seal_failure")
        target.mkdir()
        (target / "measurements.age").write_bytes(b"fictional-ciphertext")
        (target / "receipt.json").write_text("{}")

    monkeypatch.setattr(closeout.evidence, "seal", seal)
    monkeypatch.setattr(
        closeout.transport,
        "validate_sealed",
        lambda p: [str(p / "measurements.age"), str(p / "receipt.json")],
    )
    with pytest.raises((ValueError, SystemExit)):
        closeout.main()
    if seal_failed:
        assert not output.exists()
    else:
        lines = output.read_text().splitlines()
        assert len(lines) == 2
        assert lines[0].endswith("measurements.age")
        assert lines[1].endswith("receipt.json")
        assert all("raw" not in line for line in lines)
