"""Public contracts checked against real journals, repositories and API projections."""

import copy
import json
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

from clytie.backup.journal import BackupJournal
from clytie.sdk.testing import MockClock

from .test_api_management import management
from .test_backup_explicit_cleanup import fixture


def validator(kind):
    value = json.loads(Path("schemas/backup-management.schema.json").read_text(encoding="utf-8"))
    value["$ref"] = "#/$defs/" + kind
    Draft202012Validator.check_schema(value)
    return Draft202012Validator(value, format_checker=FormatChecker())


async def test_real_heavy_io_status_schema_preserves_unknown_and_rejects_private_details(tmp_path):
    from clytie.backup.io_control import IoControl

    control = IoControl(MockClock(), home=tmp_path)
    ready = await control.recheck()
    validator("heavy_io").validate(ready)
    control.responsibilities.index_path.write_text("{", encoding="utf-8")
    blocked = await control.recheck()
    validator("heavy_io").validate(blocked)
    assert blocked["state"] == "blocked"
    assert blocked["blockers"][0]["reason"] == "responsibility_evidence_unverified"
    leaked = copy.deepcopy(blocked)
    leaked["blockers"][0]["path"] = "fictional-private-location"
    assert not validator("heavy_io").is_valid(leaked)
    invented = copy.deepcopy(blocked)
    invented["blockers"][0]["reason"] = "arbitrary_exception_text"
    assert not validator("heavy_io").is_valid(invented)


@pytest.mark.parametrize(
    "kind,body,valid",
    [
        ("create_request", {}, True),
        ("create_request", {"scope": "instance"}, True),
        ("create_request", {"scope": "memory", "companion_id": "companion_a"}, True),
        ("create_request", {"scope": "memory"}, False),
        ("create_request", {"scope": "instance", "companion_id": "companion_a"}, False),
        ("create_request", {"scope": "invalid"}, False),
        ("create_request", {"operation_id": "../operation"}, False),
        ("verify_request", {"backup_id": "package_a"}, True),
        ("verify_request", {"backup_id": "../package"}, False),
        ("verify_request", {}, False),
        ("restore_plan_request", {"source": "package", "key_path": "key", "target": "new"}, True),
        ("restore_plan_request", {"source": "package", "key_path": "", "target": "new"}, False),
        ("restore_apply_request", {"plan_id": "a" * 32, "confirm": True}, True),
        ("restore_apply_request", {"plan_id": "*"}, False),
        ("source_repository", {"root": "source", "backup_id": "package_a"}, True),
        ("source_repository", {"root": "source", "backup_id": "package_a", "delete": True}, False),
        (
            "source_repository",
            {"root": "source", "backup_id": "package_a", "repository_id": "wrong"},
            False,
        ),
    ],
)
def test_request_contracts(kind, body, valid):
    assert validator(kind).is_valid(body) is valid


def test_actual_journal_public_results_and_reason_are_bounded(tmp_path):
    clock = MockClock()
    journal, _ = BackupJournal.accept(tmp_path, clock, uuid4().hex, "create", {"scope": "instance"})
    validator("operation").validate(journal.public())
    journal.phase("capturing", reason="capture_busy")
    public = journal.public()
    validator("operation").validate(public)
    assert public["reason"] == "capture_busy"
    assert "arguments" not in public and "executor" not in public
    journal.phase("failed", reason="fictional credential text must stay private")
    public = journal.public()
    assert public["reason"] == "worker_failed"
    validator("operation").validate(public)
    bad = dict(public, arguments={"key_path": "fictional-private-file"})
    assert not validator("operation").is_valid(bad)
    bad = dict(public, reason="fictional_unknown_code")
    assert not validator("operation").is_valid(bad)


def test_actual_committed_creation_preserves_independent_failure_axes(tmp_path):
    from clytie.backup.creation import _result

    store, _ = fixture(tmp_path)
    result = _result(store.get("package_0"))
    result.update(
        retention="failed",
        retention_reason="space_insufficient",
        mirror="failed",
        mirror_reason="mirror_unavailable",
    )
    journal, _ = BackupJournal.accept(store.home, store.clock, uuid4().hex, "create", {})
    journal.commit(result)
    validator("operation").validate(journal.public())
    assert journal.public()["result"]["creation"] == "complete"
    broken = copy.deepcopy(journal.public())
    broken["result"]["retention_reason"] = "arbitrary_error_text"
    assert not validator("operation").is_valid(broken)


async def test_actual_list_status_and_preview_projections(tmp_path):
    from clytie.backup.previews import PreviewCatalog
    from clytie.supervisor.common import Audit

    store, _ = fixture(tmp_path)
    api = management(store.home)
    api.svc.audit = Audit(store.home, api.clock)
    journal, _ = BackupJournal.accept(
        store.home, api.clock, uuid4().hex, "verify", {"backup_id": "package_0"}
    )
    journal.commit({"backup_id": "package_0", "verification": "passed"})
    value = await api.call(
        "/backup/operations/<operation_id>", {"operation_id": journal.value["id"]}, {}, {}, "GET"
    )
    validator("operation").validate(value)
    value = await api.call("/backup/list", {}, {}, {}, "GET")
    validator("list_response").validate(value)
    assert len(value["backups"]) == 5
    value = await api.call("/backup/status", {}, {}, {}, "GET")
    validator("status").validate(value)
    assert value["primary"]["count"] == 5
    assert value["operations"][0]["phase"] == "committed"
    assert not validator("status").is_valid(dict(value, arguments={"internal": True}))
    binding = store.repository.resolve("package_0")
    validator("source_repository_binding").validate(binding)
    catalog = PreviewCatalog(store.home, store.clock)
    catalog.bind_source(binding["source"], binding, confirmed=True)
    catalog.reference(binding["source"], "generation_schema", kind="generation")
    plan_id = uuid4().hex
    catalog.publish(
        "instance",
        plan_id,
        binding["source"],
        "generation_schema",
        target="new_target",
        fingerprint="fictional",
    )
    preview = await api.call(
        "/backup/plans/<kind>/<plan_id>", {"kind": "instance", "plan_id": plan_id}, {}, {}, "GET"
    )
    validator("preview").validate(preview)
    assert preview["state"] == "ready"
    cancelled = await api.call(
        "/backup/plans/<kind>/<plan_id>/cancel",
        {"kind": "instance", "plan_id": plan_id},
        {},
        {},
        "POST",
    )
    validator("preview").validate(cancelled)
    assert cancelled["state"] == "cancelled"


async def test_http_invalid_requests_and_source_confirmation_use_documented_envelope(tmp_path):
    from aiohttp import ClientSession

    from clytie.api.server import Server
    from clytie.supervisor.common import Audit

    store, _ = fixture(tmp_path)
    api = management(store.home)
    server = Server(store.home, api.clock, api, Audit(store.home, api.clock), {"port": 0})
    await server.start()
    try:
        async with ClientSession(trust_env=False) as client:
            headers = {}
            headers["Authorization"] = "Bearer " + server.auth.token
            base = f"http://127.0.0.1:{server.port}/api/v1/backup"
            for endpoint, payload in [
                ("create", {"scope": "invalid"}),
                ("verify", {"backup_id": "../other"}),
                ("restore/plan", {"source": "source"}),
            ]:
                async with client.post(
                    base + "/" + endpoint, json=payload, headers=headers
                ) as response:
                    body = await response.json()
                    assert response.status == 400
                    validator("error_response").validate(body)
                    assert body["error"]["code"] == "invalid_request"
            source = store.repository.resolve("package_0")
            payload = {
                "source": source["source"],
                "key_path": "fictional_key",
                "target": "new_target",
                "source_repository": {"root": source["root"], "backup_id": "package_0"},
            }
            validator("restore_plan_request").validate(payload)
            async with client.post(
                base + "/restore/plan", json=payload, headers=headers
            ) as response:
                body = await response.json()
                assert response.status == 428
                validator("error_response").validate(body)
                assert body["error"]["code"] == "source_repository_confirmation_required"
                validator("source_repository_binding").validate(body["result"]["source_repository"])
    finally:
        await server.close()


async def test_real_space_guard_status_is_numeric_and_blocks_path_leaks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from clytie.backup import space
    from clytie.supervisor.common import Audit

    from .test_backup_space import setup

    home, _, guard, plan, clock = setup(tmp_path)
    api = management(home)
    api.svc.audit = Audit(home, clock)
    value = await api.call("/backup/status", {}, {}, {}, "GET")
    validator("status").validate(value)
    estimate = value["space_estimates"][0]
    assert estimate["state"] == "monitoring"
    assert estimate["operation_id"] == plan["operation_id"]
    assert estimate["estimated"] is True and estimate["guaranteed"] is False
    assert estimate["failure"] is None
    assert str(home) not in json.dumps(estimate)
    for field in ("work", "wal", "probe", "path"):
        assert not validator("space_estimate").is_valid(dict(estimate, **{field: "fictional"}))
    volume_path = copy.deepcopy(estimate)
    volume_path["volumes"][0]["probe"] = str(home)
    assert not validator("space_estimate").is_valid(volume_path)
    assert not validator("space_estimate").is_valid(dict(estimate, state="completed"))
    assert not validator("space_estimate").is_valid(dict(estimate, source_bytes=-1))
    assert not validator("space_estimate").is_valid(dict(estimate, guaranteed=True))

    monkeypatch.setattr(space.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    with pytest.raises(space.SpaceError):
        guard.check(force=True)
    value = await api.call("/backup/status", {}, {}, {}, "GET")
    validator("status").validate(value)
    blocked = value["space_estimates"][0]
    assert blocked["state"] == "blocked"
    assert blocked["failure"] == {"code": "space_insufficient", "dimension": "reserve_free"}
    assert not validator("space_estimate").is_valid(dict(blocked, failure=None))
    wrong = copy.deepcopy(blocked)
    wrong["failure"]["dimension"] = "arbitrary_error"
    assert not validator("space_estimate").is_valid(wrong)

    # Corrupt only this fictional operation's evidence; API must report unknown,
    # never invent an empty list or expose internal JSON/path diagnostics.
    guard.path.write_text("invalid evidence", encoding="utf-8")
    value = await api.call("/backup/status", {}, {}, {}, "GET")
    validator("status").validate(value)
    assert value["space_estimates"] == {
        "state": "unavailable",
        "reason": "space_evidence_unverified",
    }
