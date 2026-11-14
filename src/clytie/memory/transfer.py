"""停止状态下的两阶段搬家；计划不保存明文，历史只追加。"""

import hashlib
import inspect
import json
import os
from contextlib import ExitStack, nullcontext
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from clytie.api.management_io import file_call
from clytie.api.management_io import management_command as run_sync
from clytie.api.management_journal import (
    begin_database,
    cancellation,
    checkpoint,
    finish_commit,
    publish_file,
    transaction_committed,
)
from clytie.plugin_host.locking import file_lock, process_identity
from clytie.plugin_host.snapshots import UpgradeSnapshots, contained, identifier
from clytie.plugin_host.support import HostLog
from clytie.sdk.types import ImportCandidate, MemoryRecord

from . import origins
from .crypto import b64, decrypt, un64
from .package import TABLES, open_database, snapshot, unpack, write_temporary
from .store import BASIS, canonical, content_hash, fingerprint


def database_fingerprint(store):
    tables = (*TABLES, "keywords", "vectors", "fade", "recollect_progress", *origins.TABLES)
    return fingerprint(
        {
            name: sorted(
                [list(row) for row in store.db.execute(f"SELECT * FROM {name}")]
                if store.has_table(name)
                else [],
                key=canonical,
            )
            for name in tables
        }
    )


def source_fingerprint(source):
    source = Path(source)
    if not source.exists():
        raise ValueError("源副本不存在")
    root = source if source.is_dir() else source.parent
    contained(root, source)
    if source.is_file():
        hashed = hashlib.sha256()
        with source.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                hashed.update(block)
        return hashed.hexdigest()
    rows = []
    for path in sorted(source.rglob("*")):
        contained(root, path)
        if path.is_file():
            data = path.read_bytes()
            rows.append(
                (path.relative_to(root).as_posix(), len(data), hashlib.sha256(data).hexdigest())
            )
        elif not path.is_dir():
            raise ValueError("unsupported source entry")
    return fingerprint(rows)


def runtime_lock(home, companion_id, operation="memory_runtime"):
    home = Path(home).resolve()
    path = contained(home, home / "companions" / identifier(companion_id) / "memory" / ".lock")
    return file_lock(path, {**process_identity(), "operation": operation})


class Transfers:
    def __init__(
        self,
        store,
        home,
        companion_id,
        *,
        importers=None,
        log=None,
        runtime_owned=False,
        source_adapter=None,
    ):
        self.store, self.home = store, Path(home).resolve()
        self.cid = identifier(companion_id)
        self.importers = importers or {}
        self.log = log or HostLog()
        self.runtime_owned = runtime_owned
        self.source_adapter = source_adapter

    def plan_path(self, plan_id):
        if len(plan_id) != 32 or any(c not in "0123456789abcdef" for c in plan_id):
            raise ValueError("invalid plan id")
        return contained(self.home, self.home / "run" / "memory-plans" / f"{plan_id}.json")

    def binding_fingerprint(self):
        return fingerprint({"instance": self.store.instance, "config": self.store.config})

    def importer_allowed(self, plugin_id, source_hash):
        if not self.store.uid:
            return
        if self.store.meta("importer_origin") != {"plugin": plugin_id, "source": source_hash}:
            raise ValueError("导入器只能写入空库或同一来源的未使用记忆库")
        imports = {
            row["id"]: json.loads(row["data"])
            for row in self.store.db.execute("SELECT * FROM imports")
        }
        allowed = set()
        for key, value in imports.items():
            if value.get("importer") == plugin_id and value.get("source_hash") == source_hash:
                allowed.add(key)
        while True:
            more = {key for key, value in imports.items() if value.get("undo") in allowed}
            if more <= allowed:
                break
            allowed |= more
        if any(
            row[0] not in allowed
            for row in self.store.db.execute("SELECT import_id FROM revisions")
        ):
            raise ValueError("导入后已有其他修订，不能再次使用导入器")

    def candidate(self, value, approve):
        if not isinstance(value, ImportCandidate):
            raise TypeError("importer must return ImportCandidate")
        people = self.store.instance.get("people", {})
        accounts = []
        if value.subject_account:
            requested = value.subject_account.model_dump()
            accounts = [
                key for key, person in people.items() if requested in person.get("accounts", [])
            ]
        conversations = []
        if value.origin_conversation:
            requested = value.origin_conversation.model_dump()
            conversations = [
                key
                for key, conversation in self.store.instance.get("conversations", {}).items()
                if all(conversation.get(k) == v for k, v in requested.items())
            ]
        subject = origin = None
        basis = "observed"
        source_type = "group"
        if len(accounts) == len(conversations) == 1:
            subject, origin = accounts[0], conversations[0]
            scene = self.store.instance["conversations"][origin]["scene"]
            owner = people[subject].get("role") == "owner"
            source_type = (
                "private"
                if scene == "owner_private"
                else "owner_group_interaction"
                if owner
                else "group"
            )
            if approve and owner:
                basis = value.proposed_basis
        data = value.model_dump(
            exclude={"subject_account", "origin_conversation", "proposed_basis"}
        )
        return MemoryRecord(
            **data,
            revision=1,
            subject=subject,
            origin_conversation_id=origin,
            basis=basis,
            write_mode="confirmed" if basis == "owner_confirmed" else "auto",
            source_type=source_type,
            decay={"weight": 1, "frozen": False},
        )

    async def _load(
        self, source, *, passphrase=None, recovery_code=None, importer=None, approve=False
    ):
        source = Path(source).resolve()
        before = await file_call(self.store, source_fingerprint, source)
        histories, source_audit, foreign_history = {}, [], []
        bindings = {}
        proposals = {key: 0 for key in BASIS}
        if importer:
            await run_sync(self.store, self.importer_allowed, importer, before)
            implementation = self.importers[importer]
            detected = implementation.detect(source)
            if inspect.isawaitable(detected):
                detected = await detected
            if not detected:
                raise ValueError("导入器不支持该源副本")
            records = {}
            async for candidate in implementation.read(source, cancellation()):
                await run_sync(self.store, checkpoint)
                record = self.candidate(candidate, approve)
                if record.record_id in records:
                    raise ValueError("导入候选编号重复")
                records[record.record_id] = record
                proposals[candidate.proposed_basis] += 1
            if not records:
                raise ValueError("导入器没有产生记录")
            manifest = {
                "companion_uid": await run_sync(self.store, lambda: self.store.uid),
                "record_count": len(records),
            }
        else:
            return await file_call(
                self.store,
                self._load_package,
                source,
                before,
                passphrase=passphrase,
                recovery_code=recovery_code,
            )
        if await file_call(self.store, source_fingerprint, source) != before:
            raise ValueError("源数据发生变化，请重新生成计划")
        return dict(
            bindings=bindings,
            records=records,
            manifest=manifest,
            histories=histories,
            audit=source_audit,
            hash=before,
            proposals=proposals,
            foreign_history=foreign_history,
            fade={},
        )

    def _load_package(self, source, before, *, passphrase=None, recovery_code=None):
        histories, source_audit, foreign_history = {}, [], []
        bindings = {}
        proposals = {key: 0 for key in BASIS}
        resources = ExitStack()
        fades = {}
        try:
            if self.source_adapter is not None:
                if passphrase is not None or recovery_code is not None:
                    raise ValueError("backup_source_credentials_invalid")
                manifest, db = resources.enter_context(self.source_adapter.open(source))
            else:
                raw = decrypt(
                    source.read_bytes(), passphrase=passphrase, recovery_code=recovery_code
                )
                manifest, db = unpack(raw, self.home, self.cid)
                resources.callback(db.close)
            if manifest["version"] == 3:
                registry = {
                    r["key"]: {k: r[k] for k in origins.FIELDS}
                    for r in db.execute("SELECT * FROM conversation_registry")
                }
                bindings = {
                    (r["record_id"], r["revision"]): registry[r["registry_key"]]
                    for r in db.execute("SELECT * FROM revision_origins")
                }
                fades = {row["record_id"]: dict(row) for row in db.execute("SELECT * FROM fade")}
            records = {
                row["id"]: MemoryRecord.model_validate_json(row["data"])
                for row in db.execute("SELECT * FROM records")
            }
            for row in db.execute("SELECT * FROM revisions ORDER BY record_id,revision"):
                histories.setdefault(row["record_id"], []).append(dict(row))
            source_audit = [
                json.loads(row[0]) for row in db.execute("SELECT data FROM audit ORDER BY id")
            ]
            foreign_history = [dict(row) for row in db.execute("SELECT * FROM foreign_revisions")]
        finally:
            resources.close()
        if source_fingerprint(source) != before:
            raise ValueError("源数据发生变化，请重新生成计划")
        return dict(
            bindings=bindings,
            records=records,
            manifest=manifest,
            histories=histories,
            audit=source_audit,
            hash=before,
            proposals=proposals,
            foreign_history=foreign_history,
            fade=fades,
        )

    def _normalized(self, record, *, preserve_origin=False):
        if preserve_origin:
            return record
        if (
            record.origin_conversation_id is not None
            and record.origin_conversation_id not in self.store.instance.get("conversations", {})
        ):
            return record.model_copy(update={"origin_conversation_id": None})
        return record

    def _actions(self, loaded, mode, choices, importer):
        records = loaded["records"]
        if not any(not r.retracted for r in records.values()) and self.store.all_records():
            raise ValueError("不能用空库覆盖已有记忆")
        rows, targets = [], {}
        seen_packages = self.store.meta("merged_packages", [])
        for key, source in sorted(records.items()):
            current = self.store.get_raw(key)
            target = self._normalized(
                source, preserve_origin=loaded["manifest"].get("version") == 3
            )
            action = "add" if current is None else "update"
            preserve = False
            remote_binding = (
                origins.configured(self.store, target.origin_conversation_id)
                if importer
                else loaded["bindings"].get((key, source.revision))
            )
            local_binding = origins.binding(self.store, current)
            if mode == "merge" and loaded["manifest"].get("version", 0) < 3 and local_binding:
                target = target.model_copy(
                    update={"origin_conversation_id": current.origin_conversation_id}
                )
            identity_conflict = (
                current is not None
                and origins.identity(remote_binding) != origins.identity(local_binding)
                and remote_binding is not None
            )
            if mode == "merge" and loaded["hash"] in seen_packages:
                action = "skip"
            elif mode == "merge" and identity_conflict:
                action = "conflict"
            elif (
                current and content_hash(current) == content_hash(target) and not identity_conflict
            ):
                action = "skip"
            elif current and mode == "merge":
                local_rows = list(
                    self.store.db.execute(
                        "SELECT hash,data FROM revisions WHERE record_id=? ORDER BY revision",
                        (key,),
                    )
                )
                remote_rows = loaded["histories"][key]
                local_chain = self._comparison_chain(local_rows)
                remote_chain = self._comparison_chain(remote_rows)
                if remote_chain == local_chain[: len(remote_chain)]:
                    action = "skip"
                elif local_chain == remote_chain[: len(local_chain)]:
                    original = [r["hash"] for r in local_rows]
                    preserve = original == [r["hash"] for r in remote_rows[: len(original)]]
                else:
                    action = "conflict"
            elif current is None and not importer:
                preserve = True
            choice = choices.get(key)
            if choice not in {None, "current", "package"}:
                raise ValueError("invalid conflict choice")
            selected = action != "skip" and (action != "conflict" or choice == "package")
            downgrade = bool(selected and current and BASIS[target.basis] < BASIS[current.basis])
            rows.append(
                dict(
                    record_id=key,
                    action=action,
                    choice=choice,
                    visibility=target.visibility,
                    downgrade=downgrade,
                    newer=bool(current and current.updated_at > source.updated_at),
                )
            )
            targets[key] = (target, preserve)
        if mode == "restore":
            for current in self.store.all_records():
                if current.record_id not in records and not current.retracted:
                    rows.append(
                        dict(
                            record_id=current.record_id,
                            action="retract",
                            choice=None,
                            visibility=current.visibility,
                            downgrade=False,
                            newer=False,
                        )
                    )
                    targets[current.record_id] = (
                        current.model_copy(update={"retracted": True}),
                        False,
                    )
        return rows, targets

    def _comparison_chain(self, rows):
        # 仅用于合并判定；原始 hash/parent 和包历史始终保留来源字段。
        chain = []
        for row in rows:
            value = content_hash(self._normalized(MemoryRecord.model_validate_json(row["data"])))
            if not chain or chain[-1] != value:
                chain.append(value)
        return chain

    async def plan(self, source, **options):
        if self.source_adapter is not None:
            await file_call(self.store, self.source_adapter.begin, source)
        try:
            return await self._plan(source, **options)
        except BaseException:
            if self.source_adapter is not None:
                await file_call(self.store, self.source_adapter.failed_generation, _cleanup=True)
            raise

    async def _plan(
        self,
        source,
        *,
        mode,
        passphrase=None,
        recovery_code=None,
        choices=None,
        confirm_identity=False,
        importer=None,
        approve_proposals=False,
        show_content=False,
    ):
        if mode not in {"new", "restore", "merge"}:
            raise ValueError("invalid import mode")
        if importer and mode != "new":
            raise ValueError("导入器只支持 new")
        before = await run_sync(self.store, lambda: database_fingerprint(self.store))
        loaded = await self._load(
            source,
            passphrase=passphrase,
            recovery_code=recovery_code,
            importer=importer,
            approve=approve_proposals,
        )
        return await run_sync(
            self.store,
            self._plan_loaded,
            source,
            loaded,
            before,
            mode=mode,
            choices=choices,
            confirm_identity=confirm_identity,
            importer=importer,
            approve_proposals=approve_proposals,
            show_content=show_content,
        )

    def _plan_loaded(
        self,
        source,
        loaded,
        before,
        *,
        mode,
        choices,
        confirm_identity,
        importer,
        approve_proposals,
        show_content,
    ):
        uid = self.store.uid
        if not importer:
            if uid and uid != loaded["manifest"]["companion_uid"]:
                raise ValueError("这不是她的记忆")
            if (mode == "new") != (uid is None):
                raise ValueError("空库只能 new；已有身份不能 new")
        rows, targets = self._actions(loaded, mode, choices or {}, importer)
        if database_fingerprint(self.store) != before:
            raise ValueError("记忆库发生变化，请重新生成计划")
        plan = dict(
            id=uuid4().hex,
            companion_id=self.cid,
            kind="import",
            mode=mode,
            source=str(Path(source).resolve()),
            source_hash=loaded["hash"],
            candidates=fingerprint(
                {key: r.model_dump(mode="json") for key, (r, _) in targets.items()}
            ),
            database=before,
            target_exists=self.store.path.exists(),
            configuration=self.binding_fingerprint(),
            importer=importer,
            approve_proposals=approve_proposals,
            confirm_identity=confirm_identity,
            choices=choices or {},
            records=rows,
            proposals=loaded["proposals"],
            manifest=loaded["manifest"],
            warnings=["来源包未记录 Clytie 版本（版本未知，旧清单格式）"]
            if not importer and loaded["manifest"]["version"] == 1
            else [],
            repeated_merge_count=len(rows)
            if mode == "merge" and loaded["hash"] in self.store.meta("merged_packages", [])
            else 0,
            repeated_merge_message=f"这个包已经合并过，{len(rows)} 条按上次的选择保留"
            if mode == "merge" and loaded["hash"] in self.store.meta("merged_packages", [])
            else None,
            downgrade_count=sum(r["downgrade"] for r in rows),
            reimport=bool(importer and uid),
        )
        if self.source_adapter is not None:
            plan.update(source_type="backup", backup_binding=self.source_adapter.binding)
            self.source_adapter.publish(plan, self.plan_path(plan["id"]))
        else:
            publish_file(
                self.plan_path(plan["id"]), canonical(plan).encode(), {"plan_id": plan["id"]}
            )
        preview = dict(plan)
        if show_content:
            preview["content"] = {
                key: {
                    "package": r.content,
                    "current": self.store.get_raw(key).content if self.store.get_raw(key) else None,
                }
                for key, r in loaded["records"].items()
            }
        return preview

    def _snapshot(self):
        checkpoint()
        raw = snapshot(self.store, self.home, self.cid)
        return self._snapshot_raw(raw)

    def _snapshot_raw(self, raw):
        key = UpgradeSnapshots(self.home, self.store.clock, self.log)._key(create=True)
        header = {
            "version": 1,
            "kind": "memory",
            "companion_id": self.cid,
            "fingerprint": hashlib.sha256(key).hexdigest(),
        }
        nonce = os.urandom(12)
        encrypted = AESGCM(key).encrypt(nonce, raw, canonical(header).encode())
        path = contained(
            self.home,
            self.home
            / "backups"
            / "memory"
            / self.cid
            / (
                "pre-import-"
                + self.store.clock.now().strftime("%Y%m%dT%H%M%S%fZ")
                + "-"
                + uuid4().hex
                + ".enc"
            ),
        )
        pending = path.with_name(path.name + "." + uuid4().hex + ".pending")
        try:
            write_temporary(
                pending,
                canonical(dict(header=header, nonce=b64(nonce), data=b64(encrypted))).encode(),
            )
            saved = json.loads(pending.read_bytes())
            checked = AESGCM(key).decrypt(
                un64(saved["nonce"]), un64(saved["data"]), canonical(saved["header"]).encode()
            )
            if checked != raw:
                raise ValueError("导入前快照校验失败")
            from clytie.backup.native_sqlite import validation_stage
            from clytie.backup.operation_slot import current_slot

            native = self.source_adapter is not None and current_slot() is not None
            with validation_stage() if native else nullcontext():
                db = open_database(checked, self.home, self.cid, native=native)
                db.close()
            from clytie.api.management_journal import current

            journal = current.get()
            if journal is not None:
                journal.snapshot(path, pending.read_bytes())
            pending.replace(path)
        finally:
            if pending.exists():
                pending.unlink()
        checkpoint()
        return path

    def _retention(self):
        directory = contained(self.home, self.home / "backups" / "memory" / self.cid)
        files = sorted(
            (
                p
                for p in directory.iterdir()
                if p.name.startswith("pre-import-") and p.suffix == ".enc"
            ),
            key=lambda p: (p.stat().st_mtime_ns, p.name),
            reverse=True,
        )
        for path in files[5:]:
            contained(self.home, path)
            path.unlink()
            with self.store.transaction():
                self.store.audit("memory_snapshot_removed", file=path.name)

    async def apply(self, plan_id, *, passphrase=None, recovery_code=None):
        plan = await file_call(self.store, lambda: json.loads(self.plan_path(plan_id).read_bytes()))
        if plan.get("source_type") == "backup" and (
            self.source_adapter is None or self.source_adapter.binding != plan.get("backup_binding")
        ):
            raise ValueError("backup_source_adapter_required")
        if plan["companion_id"] != self.cid or plan["id"] != plan_id:
            raise ValueError("计划目标不匹配")
        from contextlib import nullcontext

        with (
            nullcontext()
            if self.runtime_owned
            else runtime_lock(self.home, self.cid, "memory_import")
        ):
            await run_sync(self.store, self._validate_plan, plan)
            if plan["kind"] == "undo":
                return await run_sync(self.store, self._apply_validated_undo, plan)
            loaded = await self._load(
                plan["source"],
                passphrase=passphrase,
                recovery_code=recovery_code,
                importer=plan["importer"],
                approve=plan["approve_proposals"],
            )
            from clytie.backup.operation_slot import current_slot
            from clytie.storage.handle import StoreHandle

            if isinstance(self.store, StoreHandle) or (
                self.source_adapter is not None and current_slot() is not None
            ):
                from clytie.api.management_io import owned_snapshot
                from clytie.memory.store import MemoryStore

                await run_sync(self.store, self._prepare_live)
                metadata = dict(
                    clock=self.store.clock,
                    config=self.store.config,
                    instance=self.store.instance,
                    readonly=True,
                )

                def save_snapshot(path):
                    source = MemoryStore(path, **metadata)
                    try:
                        if database_fingerprint(source) != plan["database"]:
                            raise ValueError("计划失效，请重新生成")
                        raw = snapshot(source, self.home, self.cid)
                        return self._snapshot_raw(raw)
                    finally:
                        source.close()

                async with owned_snapshot(self.store, self.home, self.cid) as path:
                    backup = await file_call(self.store, save_snapshot, path)
                return await run_sync(self.store, self._apply_loaded, plan, loaded, backup=backup)
            return await run_sync(self.store, self._apply_loaded, plan, loaded)

    def _prepare_live(self):
        from .migration import migrate

        begin_database(self.store, "materializing")
        self.store.materialize()
        migrate(self.store, self.home, self.cid, self.log)

    def _validate_plan(self, plan):
        if self.store.transient and self.store.path.exists():
            raise ValueError("计划失效，记忆库已建立")
        if (
            plan["database"] != database_fingerprint(self.store)
            or plan["configuration"] != self.binding_fingerprint()
        ):
            raise ValueError("计划失效，请重新生成")

    def _apply_validated_undo(self, plan):
        self._validate_plan(plan)
        return self._apply_undo(plan)

    def _apply_loaded(self, plan, loaded, *, backup=None):
        self._validate_plan(plan)
        if loaded["hash"] != plan["source_hash"]:
            raise ValueError("计划失效，源数据发生变化")
        rows, targets = self._actions(loaded, plan["mode"], plan["choices"], plan["importer"])
        if plan["candidates"] != fingerprint(
            {key: r.model_dump(mode="json") for key, (r, _) in targets.items()}
        ) or plan["database"] != database_fingerprint(self.store):
            raise ValueError("计划失效，候选或记忆库发生变化")
        if rows != plan["records"]:
            raise ValueError("计划动作变化")
        if not self.store.uid and not plan["confirm_identity"]:
            raise ValueError("必须确认这是她的记忆")
        if any(r["action"] == "conflict" and r["choice"] is None for r in rows):
            raise ValueError("冲突尚未选择")
        if backup is None:
            self._prepare_live()
            backup = self._snapshot()
        import_id, changes = uuid4().hex, {}
        begin_database(self.store)
        with self.store.transaction():
            if not self.store.uid:
                self.store.set_meta(
                    "companion_uid", loaded["manifest"]["companion_uid"] or uuid4().hex
                )
                if plan["importer"]:
                    self.store.set_meta(
                        "importer_origin",
                        {"plugin": plan["importer"], "source": loaded["hash"]},
                    )
            for row in rows:
                record_id = row["record_id"]
                if row["action"] == "skip" or (
                    row["action"] == "conflict" and row["choice"] == "current"
                ):
                    continue
                target, preserve = targets[record_id]
                previous = self.store.get_raw(record_id)
                before_origin = origins.binding(self.store, previous)
                source = loaded["records"].get(record_id, previous)
                remote_origin = (
                    origins.configured(self.store, target.origin_conversation_id)
                    if plan["importer"]
                    else loaded["bindings"].get((record_id, source.revision))
                    if record_id in loaded["records"]
                    else before_origin
                )
                if (
                    plan["mode"] == "merge"
                    and not plan["importer"]
                    and loaded["manifest"].get("version", 0) < 3
                    and before_origin
                ):
                    remote_origin = before_origin
                if preserve:
                    for revision in loaded["histories"].get(record_id, []):
                        if previous and revision["revision"] <= previous.revision:
                            continue
                        self.store._save(
                            MemoryRecord.model_validate_json(revision["data"]),
                            operator=revision["operator"],
                            trace=revision["trace"],
                            reason=revision["reason"],
                            timestamp=revision["at"],
                            import_id=import_id,
                            origin_binding=loaded["bindings"].get(
                                (record_id, revision["revision"]),
                                remote_origin
                                if loaded["manifest"].get("version", 0) < 3
                                and plan["mode"] == "merge"
                                else None,
                            ),
                        )
                    current = self.store.get_raw(record_id)
                    if current and content_hash(current) != content_hash(target):
                        target = target.model_copy(update={"revision": current.revision + 1})
                        self.store._save(
                            target,
                            operator="import",
                            import_id=import_id,
                            origin_binding=remote_origin,
                        )
                else:
                    target = target.model_copy(
                        update={"revision": previous.revision + 1 if previous else 1}
                    )
                    self.store._save(
                        target,
                        operator="import",
                        import_id=import_id,
                        origin_binding=remote_origin,
                    )
                    for revision in loaded["histories"].get(record_id, []):
                        self.store.db.execute(
                            "INSERT OR IGNORE INTO foreign_revisions VALUES(?,?,?,?)",
                            (
                                loaded["hash"],
                                record_id,
                                revision["revision"],
                                canonical(revision),
                            ),
                        )
                changes[record_id] = {
                    "before_origin": before_origin,
                    "after_origin": origins.binding(self.store, self.store.get_raw(record_id)),
                    "before": previous.model_dump(mode="json") if previous else None,
                    "after": self.store.get_raw(record_id).model_dump(mode="json"),
                }
            from .fade import state as fade_state

            for row in rows:
                rid = row["record_id"]
                current = self.store.get_raw(rid)
                if current is None:
                    continue
                remote = loaded["fade"].get(rid)
                if remote:
                    at, half = fade_state(self.store, current)
                    if plan["mode"] == "merge":
                        at = max(at, datetime.fromisoformat(remote["last_recalled_at"]))
                        half = max(half, remote["half_life_days"])
                    else:
                        at, half = (
                            datetime.fromisoformat(remote["last_recalled_at"]),
                            remote["half_life_days"],
                        )
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO fade VALUES(?,?,?)", (rid, at.isoformat(), half)
                    )
                elif rid in changes:
                    self.store.db.execute(
                        "INSERT OR REPLACE INTO fade VALUES(?,?,?)",
                        (
                            rid,
                            self.store.clock.now().isoformat(),
                            self.store.config.get("memory", {})
                            .get("fade", {})
                            .get("half_life_days", 30),
                        ),
                    )
            self.store.db.execute(
                "DELETE FROM recollect_progress WHERE conversation_id != '!state'"
            )
            origins.track(self.store)
            if plan["mode"] == "merge":
                self.store.set_meta(
                    "merged_packages",
                    sorted(set(self.store.meta("merged_packages", [])) | {loaded["hash"]}),
                )
            for source_event in loaded["audit"]:
                self.store.audit(
                    "memory_imported_audit", source_package=loaded["hash"], event=source_event
                )
            for revision in loaded["foreign_history"]:
                self.store.db.execute(
                    "INSERT OR IGNORE INTO foreign_revisions VALUES(?,?,?,?)",
                    (
                        revision["package"],
                        revision["record_id"],
                        revision["revision"],
                        revision["data"],
                    ),
                )
            self._record_import(
                import_id,
                changes,
                importer=plan["importer"],
                source_hash=loaded["hash"],
                snapshot=backup.name,
            )
        finish_commit({"import_id": import_id})
        self._retention()
        return import_id

    def _record_import(self, import_id, changes, **metadata):
        value = dict(at=self.store.clock.now().isoformat(), changes=changes, **metadata)
        self.store.db.execute("INSERT INTO imports VALUES(?,?)", (import_id, canonical(value)))
        self.store.mark_backup_dirty()
        self.store.audit("memory_import", import_id=import_id, changed=len(changes))
        transaction_committed(self.store, {"import_id": import_id})

    def _undo_data(self, import_id, choices):
        row = self.store.db.execute("SELECT data FROM imports WHERE id=?", (import_id,)).fetchone()
        if not row:
            raise ValueError("找不到导入编号")
        original = json.loads(row[0])
        rows, targets = [], {}
        for key, change in original["changes"].items():
            current = self.store.get_raw(key)
            if current is None:
                raise ValueError("导入记录缺失")
            target = (
                MemoryRecord.model_validate(change["before"])
                if change["before"]
                else current.model_copy(update={"retracted": True})
            )
            conflict = current.model_dump(mode="json") != change["after"] or origins.binding(
                self.store, current
            ) != change.get("after_origin")
            choice = choices.get(key)
            if choice not in {None, "current", "package"}:
                raise ValueError("invalid conflict choice")
            rows.append(
                dict(
                    record_id=key,
                    action="conflict" if conflict else "restore" if change["before"] else "retract",
                    choice=choice,
                    visibility=target.visibility,
                    downgrade=BASIS[target.basis] < BASIS[current.basis]
                    and (not conflict or choice == "package"),
                )
            )
            targets[key] = target
        return original, rows, targets

    def undo(self, import_id, *, choices=None, show_content=False):
        original, rows, targets = self._undo_data(import_id, choices or {})
        days = max(0, (self.store.clock.now() - datetime.fromisoformat(original["at"])).days)
        counts = {
            kind: sum(r["action"] == kind for r in rows)
            for kind in ("retract", "restore", "conflict")
        }
        plan = dict(
            id=uuid4().hex,
            companion_id=self.cid,
            kind="undo",
            undo=import_id,
            database=database_fingerprint(self.store),
            configuration=self.binding_fingerprint(),
            choices=choices or {},
            records=rows,
            imported_at=original["at"],
            summary=f"这是 {days} 天前的导入，将撤回 {counts['retract']} 条、改回 {counts['restore']} 条、冲突 {counts['conflict']} 条",
            days_ago=days,
            counts=counts,
            downgrade_count=sum(r["downgrade"] for r in rows),
        )
        publish_file(self.plan_path(plan["id"]), canonical(plan).encode(), {"plan_id": plan["id"]})
        preview = dict(plan)
        if show_content:
            preview["content"] = {
                key: {"package": r.content, "current": self.store.get_raw(key).content}
                for key, r in targets.items()
            }
        return preview

    def _apply_undo(self, plan):
        from .migration import migrate

        migrate(self.store, self.home, self.cid, self.log)
        original, rows, targets = self._undo_data(plan["undo"], plan["choices"])
        if rows != plan["records"] or any(
            r["action"] == "conflict" and r["choice"] is None for r in rows
        ):
            raise ValueError("撤销计划失效或冲突尚未选择")
        backup = self._snapshot()
        import_id, changes = uuid4().hex, {}
        begin_database(self.store)
        with self.store.transaction():
            for row in rows:
                if row["action"] == "conflict" and row["choice"] == "current":
                    continue
                key = row["record_id"]
                current = self.store.get_raw(key)
                target = targets[key].model_copy(update={"revision": current.revision + 1})
                before_origin = origins.binding(self.store, current)
                change = original["changes"][key]
                restored_origin = change.get("before_origin") if change["before"] else before_origin
                self.store._save(
                    target,
                    operator="undo_import",
                    import_id=import_id,
                    origin_binding=restored_origin,
                )
                changes[key] = {
                    "before_origin": before_origin,
                    "after_origin": restored_origin,
                    "before": current.model_dump(mode="json"),
                    "after": target.model_dump(mode="json"),
                }
            self.store.db.execute(
                "DELETE FROM recollect_progress WHERE conversation_id != '!state'"
            )
            origins.track(self.store)
            self._record_import(import_id, changes, undo=plan["undo"], snapshot=backup.name)
        finish_commit({"import_id": import_id})
        self._retention()
        return import_id
