"""Preparation orchestration only; false executors never count as geometry evidence."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.measurement_tools import load
from tests.test_measurement_preparation_transport import ci_fixture

generator = load("measurement_preparation_generate")


def setup(tmp_path, monkeypatch, fault=None):
    source, binding, _, env, root, version = ci_fixture(tmp_path, monkeypatch)
    extra = "scripts/measurement_preparation_generate.py"
    (root / extra).write_bytes(b"fictional_generator\n")
    names = (*generator.launch.TOOL_FILES, extra)
    monkeypatch.setattr(generator.launch, "TOOL_FILES", names)
    monkeypatch.setattr(generator.platform, "system", lambda: "Windows")
    oldgit = generator.versions._git

    def git(repository, *args):
        if args == ("diff", "HEAD", "--name-only"):
            return b"fictional_modified.py" if fault == "dirty" else b""
        return oldgit(repository, *args)

    monkeypatch.setattr(generator.versions, "_git", git)
    before = (version / "source-inventory.json").read_bytes()

    def prepare(repository, destination, **kw):
        assert kw == dict(mode="candidate", candidate=binding["candidate"])
        destination.mkdir()
        (destination / "source").mkdir()
        (destination / "source/fictional.py").write_bytes(
            (version / "source/fictional.py").read_bytes()
        )
        (destination / "source-inventory.json").write_bytes(before)

    monkeypatch.setattr(generator.versions, "prepare", prepare)
    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        target = Path(args[args.index("--target") + 1])
        assert "build" in args and kwargs["cwd"] == root
        assert kwargs["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
        assert kwargs["env"]["PYTHONUTF8"] == "1"
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.STDOUT
        target.mkdir()
        (target / "geometry.json").write_bytes((source / "geometry.json").read_bytes())
        runtime = generator.runtime()
        if fault == "runtime":
            runtime["sqlite"] = "0.0.0"
        (target / "runtime.json").write_bytes(generator.prep.canonical(runtime))
        if fault == "tool":
            (root / extra).write_bytes(b"changed")
        if fault == "source":
            (Path(args[args.index("--version-directory") + 1]) / "source/fictional.py").write_bytes(
                b"changed"
            )
        return subprocess.CompletedProcess(
            args, 1 if fault == "child" else 0, b"fictional preparation diagnostic\n"
        )

    return source, binding, env, root, calls, execute


def test_preparation_binds_actual_material_and_retains_only_two_raw_files(tmp_path, monkeypatch):
    source, binding, env, root, calls, execute = setup(tmp_path, monkeypatch)
    target = tmp_path / "prepared"
    actual = generator.prepare(root, target, binding["candidate"], environ=env, execute=execute)
    assert len(calls) == 1
    assert actual["candidate"] == binding["candidate"]
    assert set(p.name for p in (target / "raw").iterdir()) == set(generator.prep.FILES)
    assert (target / "raw/geometry.json").read_bytes() == (source / "geometry.json").read_bytes()
    assert json.loads((target / "raw/runtime.json").read_bytes()) == generator.runtime()
    record = json.loads((target / "generation.json").read_bytes())
    assert record["source_before"] == record["source_after"]
    assert record["measurement_acceptance"] == "not_applicable"
    assert (
        target / "generation-diagnostic.txt"
    ).read_bytes() == b"fictional preparation diagnostic\n"
    assert not (target / "sealed").exists()


@pytest.mark.parametrize("fault", ["dirty", "child", "tool", "source", "runtime"])
def test_generation_failure_keeps_evidence_but_has_no_approved_metadata(
    tmp_path, monkeypatch, fault
):
    _, binding, env, root, calls, execute = setup(tmp_path, monkeypatch, fault)
    target = tmp_path / "prepared"
    with pytest.raises((generator.prep.Error, ValueError)):
        generator.prepare(root, target, binding["candidate"], environ=env, execute=execute)
    assert not (target / "generation.json").exists()
    assert not (target / "expected.json").exists()
    assert len(calls) == (0 if fault == "dirty" else 1)


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_ACTIONS", "false"),
        ("GITHUB_REPOSITORY", "fictional/other"),
        ("GITHUB_JOB", "other"),
    ],
)
def test_wrong_environment_never_invokes_generator(tmp_path, monkeypatch, field, value):
    _, binding, env, root, calls, execute = setup(tmp_path, monkeypatch)
    env[field] = value
    with pytest.raises(generator.prep.Error):
        generator.prepare(
            root, tmp_path / "prepared", binding["candidate"], environ=env, execute=execute
        )
    assert not calls


@pytest.mark.asyncio
async def test_build_checks_import_ownership_and_source_after_construct(tmp_path, monkeypatch):
    actions = []
    inventory = {"fictional": "fixed"}
    source = tmp_path / "source"
    monkeypatch.setattr(
        generator.versions, "activate", lambda p: (actions.append("activate") or source, inventory)
    )
    monkeypatch.setattr(
        generator.versions, "verify", lambda p: actions.append("verify") or inventory
    )
    monkeypatch.setattr(generator.versions, "verify_imports", lambda p: actions.append("imports"))

    async def construct(target, scales, *, seed):
        assert actions == ["activate", "import", "verify"]
        assert scales == ("a", "b") and seed == 73
        actions.append("construct")
        target.mkdir()

    def import_geometry(*args):
        actions.append("import")
        return SimpleNamespace(
            construct=construct, fixture=SimpleNamespace(SCALES={"a": None, "b": None})
        )

    monkeypatch.setattr(generator.importlib, "import_module", import_geometry)
    await generator.build(tmp_path / "product", tmp_path / "geometry")
    assert actions == ["activate", "import", "verify", "construct", "imports", "verify"]
    assert json.loads((tmp_path / "geometry/runtime.json").read_bytes()) == generator.runtime()
