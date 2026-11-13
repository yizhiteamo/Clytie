"""Separate fixed product and tool commits; no formal measurement execution."""

import subprocess
from pathlib import Path

import pytest
from tests.measurement_tools import load
from tests.test_measurement_mechanism_launch import setup
from tests.test_measurement_plan import inputs, profiles

plan = load("measurement_plan")
versions = load("measurement_version")
launcher = load("measurement_mechanism_launch")
ROOT = Path(__file__).resolve().parents[1]


def test_tool_commit_is_explicit_validated_and_changes_plan_identity():
    inventory, geometry = inputs()
    old = plan.freeze_plan(plan.draft_plan("mechanism", inventory, geometry, profiles=profiles()))
    inventory["tool_commit"] = "e" * 40
    new = plan.freeze_plan(plan.draft_plan("mechanism", inventory, geometry, profiles=profiles()))
    plan.validate_plan(new)
    assert new["plan_sha256"] != old["plan_sha256"]
    assert new["runs"][0]["source_commit"] == inventory["candidate"]
    assert new["runs"][0]["execution_id"] != old["runs"][0]["execution_id"]
    inventory["tool_commit"] = "main"
    with pytest.raises(plan.PlanError):
        plan.draft_plan("mechanism", inventory, geometry, profiles=profiles())


def test_launcher_checks_tool_head_but_keeps_product_candidate(tmp_path, monkeypatch):
    data, env, directory = setup(tmp_path, monkeypatch)
    frozen = data[0]
    values = frozen["inputs"]
    values["inventory"]["tool_commit"] = "e" * 40
    frozen = plan.freeze_plan(plan.draft_plan(**values))
    observed = []
    monkeypatch.setattr(launcher.preparation, "current", lambda root, value: observed.append(value))
    proof = launcher.prep_transport.material_identity

    def identity(root, source, candidate):
        observed.append(candidate)
        return proof(root, source, candidate)

    monkeypatch.setattr(launcher.prep_transport, "material_identity", identity)
    manifest, _ = launcher.checked(
        frozen, frozen["runs"][0]["execution_id"], directory, environ=env
    )
    assert observed == ["e" * 40, values["inventory"]["candidate"]]
    assert manifest["source_commit"] == values["inventory"]["candidate"]


def test_real_archive_uses_product_commit_not_tool_checkout(tmp_path):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    product = versions.ORIGINAL
    target = tmp_path / "fixed"
    result = versions.prepare(ROOT, target, mode="candidate", candidate=product, tool_commit=head)
    assert result["candidate"] == product == result["source_commit"]
    expected = subprocess.check_output(
        ["git", "show", product + ":src/clytie/__init__.py"], cwd=ROOT
    )
    assert (target / "source/src/clytie/__init__.py").read_bytes() == expected
    versions.verify(target)
    with pytest.raises(ValueError, match="candidate_changed"):
        versions.prepare(
            ROOT, tmp_path / "wrong", mode="candidate", candidate=product, tool_commit="a" * 40
        )
    assert not (tmp_path / "wrong").exists()
