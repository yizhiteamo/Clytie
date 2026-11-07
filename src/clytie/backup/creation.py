"""创建、认证、发布和登记；恢复仅检查既有产物，绝不重新捕获。"""

import asyncio
import sqlite3
from pathlib import Path
from uuid import uuid4

import yaml

from clytie.config import repository_root
from clytie.plugin_host.snapshots import canonical, contained

from .container import pack, unpack
from .errors import BackupError, error_reason
from .keys import BackupKeys
from .mirror import MirrorManager, publish_no_replace
from .restore import LEDGER_TABLES, MEMORY_TABLES, _database, digest, identity, validate_contents
from .retention import RetentionConfig
from .source import capture_instance
from .storage import BackupStorage
from .temporary import TemporaryArtifacts


def _settings(home):
    return yaml.safe_load((home / "clytie.local.yaml").read_bytes()).get("backup", {})


def _validate(root, envelope):
    manifest = envelope["manifest"]
    if manifest["scope"] == "instance" and manifest["completeness"] == "complete":
        validate_contents(root, envelope)
        return
    members = {r["path"]: r for r in envelope["files"]}
    covered = set()
    for group in manifest["consistency_groups"]:
        if set(group["members"]) != set(group["sha256"]) or not group["epoch"]:
            raise BackupError("snapshot_inconsistent")
        for name in group["members"]:
            if (
                name in covered
                or name not in members
                or members[name]["sha256"] != group["sha256"][name]
            ):
                raise BackupError("snapshot_inconsistent")
            covered.add(name)
    if covered != set(members) or not members:
        raise BackupError("snapshot_inconsistent")
    for name in members:
        if name.endswith(".sqlite3"):
            path = contained(root, root / name)
            required = (
                MEMORY_TABLES
                if name.endswith("/memory/memory.sqlite3")
                else LEDGER_TABLES
                if name.endswith("/ledger/ledger.sqlite3")
                else None
            )
            if required is not None:
                verified = _database(path, required)
                verified.close()
            with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise BackupError("snapshot_inconsistent")
                if (
                    manifest["scope"] == "memory"
                    and db.execute("SELECT count(*) FROM recollect_progress").fetchone()[0]
                ):
                    raise BackupError("snapshot_inconsistent")


def _verify(home, package, key, work, *, expected=None, check=None):
    work.mkdir(parents=True, exist_ok=False)
    envelope = unpack(package, work, key, check=check)
    if expected is not None and canonical(envelope) != canonical(expected):
        raise BackupError("backup_evidence_mismatch")
    _validate(work, envelope)
    return envelope


def _result(row):
    return dict(
        backup_id=row["backup_id"],
        creation=row["completeness"],
        verification="passed",
        retention="pending" if not row["manual"] else "skipped",
        mirror="pending",
        restore_readiness="eligible"
        if row["scope"] == "instance" and row["completeness"] == "complete"
        else "not_applicable",
        missing_count=row["missing_count"],
        bytes=row["size"],
    )


def dispatch(home, clock, journal, controls):
    home = Path(home).resolve()
    store = BackupStorage(home, clock)
    args = journal.value["arguments"]
    operation_id = journal.value["id"]
    work = contained(home, home / "run/backup-work" / operation_id)
    temporaries = TemporaryArtifacts(home, operation_id)
    temporaries.begin()
    settings = _settings(home)
    config_hash = digest(home / "clytie.local.yaml")
    keys = BackupKeys(home, clock, repository=repository_root())
    if journal.value["operation"] == "verify":
        from .previews import PreviewCatalog

        row = store.get(args["backup_id"])
        journal.phase("verifying", artifacts=[dict(kind="backup_verify", record=row)])
        with PreviewCatalog(home, clock).protect(row["path"], operation_id):
            if not store.valid(row):
                raise BackupError("backup_evidence_mismatch")
            _verify(home, Path(row["path"]), keys._read(), work / "verify", check=controls.check)
            temporaries.register([work / "verify"])
            temporaries.cleanup()
        return dict(backup_id=row["backup_id"], verification="passed")
    keys.ensure()
    key = keys._read()
    instance_id = store.instance_id()
    store.root.mkdir(parents=True, exist_ok=True)
    with store.lock():
        started_bytes = sum(r["size"] for r in store.list() if not r["manual"])
    reserve = int(settings.get("disk", {}).get("reserve_gib", 1) * 1024**3)
    from .space import SpaceGuard, estimate

    configured = yaml.safe_load((home / "clytie.local.yaml").read_bytes())
    selected = [args["companion_id"]] if args.get("scope") == "memory" else configured["companions"]
    guard = SpaceGuard(home, operation_id, clock=clock)
    guard.start(
        estimate(
            home,
            operation_id,
            companions=selected,
            scope=args.get("scope", "instance"),
            reserve_bytes=reserve,
            destination=store.root,
            check=controls.check,
            clock=clock,
        )
    )

    def checkpoint():
        controls.check()
        guard.check()

    checkpoint()
    stage, temporary = work / "capture", work / "package.pending"
    final = store.root / (operation_id + ".clytie-backup")
    journal.phase(
        "capturing",
        artifacts=[
            dict(kind="workspace", path=str(work)),
            dict(kind="backup_expected", path=str(final), temporary=str(temporary)),
        ],
    )
    context = dict(getattr(controls, "capture_context", {}))
    captured = asyncio.run(
        capture_instance(
            home,
            clock,
            stage,
            source_instance_id=instance_id,
            scope=args.get("scope", "instance"),
            cid=args.get("companion_id"),
            epoch=operation_id,
            check=checkpoint,
            **context,
        )
    )
    guard.check(force=True)
    if not captured["files"]:
        raise BackupError("snapshot_unavailable")
    temporaries.register([stage])
    if not captured["missing"]:
        temporaries.register(captured["temporary_directories"])
    captured["manifest"].update(
        operation_id=operation_id,
        captured_at=clock.now().isoformat(),
        manual=args.get("manual", True),
        trigger_id=args.get("trigger_id"),
    )
    journal.phase("copying")
    hashed = pack(stage, temporary, key, captured["manifest"], check=checkpoint)
    journal.phase("verifying")
    expected = {
        "manifest": captured["manifest"],
        "files": sorted(captured["files"], key=lambda r: r["path"]),
    }
    # Container order follows directory traversal; compare actual file maps, not input ordering.
    envelope = _verify(home, temporary, key, work / "verified", check=checkpoint)
    temporaries.register([work / "verified"])
    if (
        envelope["manifest"] != expected["manifest"]
        or sorted(envelope["files"], key=lambda r: r["path"]) != expected["files"]
    ):
        raise BackupError("backup_evidence_mismatch")
    row = dict(
        backup_id=operation_id,
        path=str(final),
        scope=captured["manifest"]["scope"],
        group=args.get("companion_id") or instance_id,
        source_instance_id=instance_id,
        completed_at=clock.now().isoformat(),
        size=temporary.stat().st_size,
        sha256=hashed,
        identity=identity(temporary),
        manual=args.get("manual", True),
        completeness=captured["manifest"]["completeness"],
        missing_count=len(captured["missing"]),
        missing=captured["missing"],
    )
    journal.phase(
        "publishing",
        artifacts=[
            dict(
                kind="backup_package",
                record=row,
                temporary=str(temporary),
                workspace=str(work),
                started_bytes=started_bytes,
            )
        ],
        envelope=envelope,
    )
    guard.check(force=True)
    publish_no_replace(temporary, final)
    if not store.valid(row):
        raise BackupError("backup_evidence_mismatch")
    store.register(row)
    temporaries.cleanup()
    result = _result(row)

    def revalidate():
        if digest(home / "clytie.local.yaml") != config_hash:
            raise BackupError("config_changed")

    # Main commit is durable independently of optional maintenance outcomes.
    try:
        if not row["manual"]:
            config = dict(settings.get("retention", {}))
            target = config.pop("target_gib", 20)
            config["target_bytes"] = None if target is None else int(target * 1024**3)
            result["retention"] = store.rotate(
                RetentionConfig(**config),
                started_bytes=started_bytes,
                new_backup_id=operation_id,
                revalidate=revalidate,
            )
    except Exception as exc:
        result["retention"] = "failed"
        result["retention_reason"] = error_reason(exc, "retention_failed")
    result["mirror"] = "disabled"
    if settings.get("mirror", {}).get("enabled"):
        from .worker import Cancelled

        result["mirror"] = "failed"
        try:
            from .previews import PreviewCatalog

            refs = PreviewCatalog(home, clock)
            manager = MirrorManager(
                home / "supervisor/backup-mirror",
                store.root,
                clock,
                owner_id=instance_id,
                forbidden_roots=(home, repository_root()),
            )
            state = manager.state()
            repository = state["candidate"] or state["active"]
            configured = settings.get("mirror", {}).get("path")
            configured_path = Path(configured).expanduser() if configured else None
            if configured_path is not None and not configured_path.is_absolute():
                configured_path = home / configured_path
            if (
                repository is None
                or configured is None
                or configured_path.resolve() != Path(repository["path"]).resolve()
            ):
                raise BackupError("mirror_switch_pending")
            rows = {item["backup_id"]: item for item in store.list()}
            by_path = {item["path"]: item for item in rows.values()}
            copied = manager.synchronize(
                [store.record(item) for item in rows.values()],
                sha256=lambda record: rows[record.backup_id]["sha256"],
                reference=lambda record: refs.protect(record.path, "mirror-" + operation_id),
                verify=lambda path: store.valid(by_path[str(path)]),
                check=getattr(controls, "check_optional", controls.check),
            )
            getattr(controls, "check_optional", controls.check)()
            result["mirror"] = copied["status"]
            if copied["status"] == "ready":
                policy = settings.get("mirror", {})
                manager.rotate(
                    **{k: policy[k] for k in ("recent", "monthly_months", "minimum") if k in policy}
                )
        except Cancelled:
            result["mirror"] = "cancelled"
        except Exception as exc:
            result["mirror"] = "failed"
            result["mirror_reason"] = error_reason(exc, "mirror_failed")
    return result


def verify_previous(home, clock, journal, controls):
    home = Path(home).resolve()
    store = BackupStorage(home, clock)
    items = [
        a
        for a in journal.value.get("artifacts", [])
        if a["kind"] in {"backup_package", "backup_verify"}
    ]
    if len(items) != 1:
        journal.phase("failed", reason="backup_not_published")
        return
    (artifact,) = items
    row = artifact["record"]
    if not Path(row["path"]).exists():
        journal.phase("failed", reason="backup_not_published")
        return
    from .previews import PreviewCatalog

    if artifact["kind"] == "backup_package":
        # publishing evidence follows authenticated verification. Identity and hash
        # bind the published bytes to that verified candidate, before any reader
        # can obtain repository authority. No capture or publication is repeated.
        if not store.valid(row) or journal.value.get("envelope") is None:
            raise BackupError("backup_evidence_mismatch")
        store.register(row)
    with PreviewCatalog(home, clock).protect(row["path"], "verify-" + journal.value["id"]):
        if not store.valid(row):
            raise BackupError("backup_evidence_mismatch")
        work = contained(
            home, home / "run/backup-work" / journal.value["id"] / ("recheck-" + uuid4().hex)
        )
        keys = BackupKeys(home, clock, repository=repository_root())
        _verify(home, Path(row["path"]), keys._read(), work, expected=journal.value.get("envelope"))
        temporaries = TemporaryArtifacts(home, journal.value["id"])
        temporaries.register([work])
        if artifact["kind"] == "backup_package":
            store.register(row)
            result = _result(row)
            result.update(retention="not_repeated", mirror="not_repeated")
        else:
            result = dict(backup_id=row["backup_id"], verification="passed")
        temporaries.cleanup()
        journal.commit(result)
