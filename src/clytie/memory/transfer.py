"""停止状态下的两阶段搬家；计划不保存明文，历史只追加。"""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from clytie.plugin_host.locking import file_lock, process_identity
from clytie.plugin_host.snapshots import UpgradeSnapshots, atomic_write, contained, identifier
from clytie.plugin_host.support import HostLog
from clytie.sdk import CancelToken
from clytie.sdk.types import ImportCandidate, MemoryRecord

from .crypto import b64, decrypt, un64
from .package import TABLES, open_database, snapshot, unpack
from .store import BASIS, canonical, content_hash, fingerprint


def database_fingerprint(store):
    tables = (*TABLES, "keywords", "vectors")
    return fingerprint(
        {
            name: sorted(
                [list(row) for row in store.db.execute(f"SELECT * FROM {name}")], key=canonical
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
        return hashlib.sha256(source.read_bytes()).hexdigest()
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
    def __init__(self, store, home, companion_id, *, importers=None, log=None):
        self.store, self.home = store, Path(home).resolve()
        self.cid = identifier(companion_id)
        self.importers = importers or {}
        self.log = log or HostLog()

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
        before = source_fingerprint(source)
        histories, source_audit, foreign_history = {}, [], []
        proposals = {key: 0 for key in BASIS}
        if importer:
            self.importer_allowed(importer, before)
            implementation = self.importers[importer]
            if not implementation.detect(source):
                raise ValueError("导入器不支持该源副本")
            records = {}
            async for candidate in implementation.read(source, CancelToken()):
                record = self.candidate(candidate, approve)
                if record.record_id in records:
                    raise ValueError("导入候选编号重复")
                records[record.record_id] = record
                proposals[candidate.proposed_basis] += 1
            if not records:
                raise ValueError("导入器没有产生记录")
            manifest = {"companion_uid": self.store.uid, "record_count": len(records)}
        else:
            raw = decrypt(source.read_bytes(), passphrase=passphrase, recovery_code=recovery_code)
            manifest, db = unpack(raw, self.home, self.cid)
            try:
                records = {
                    row["id"]: MemoryRecord.model_validate_json(row["data"])
                    for row in db.execute("SELECT * FROM records")
                }
                for row in db.execute("SELECT * FROM revisions ORDER BY record_id,revision"):
                    histories.setdefault(row["record_id"], []).append(dict(row))
                source_audit = [
                    json.loads(row[0]) for row in db.execute("SELECT data FROM audit ORDER BY id")
                ]
                foreign_history = [
                    dict(row) for row in db.execute("SELECT * FROM foreign_revisions")
                ]
            finally:
                db.close()
        if source_fingerprint(source) != before:
            raise ValueError("源数据发生变化，请重新生成计划")
        return dict(
            records=records,
            manifest=manifest,
            histories=histories,
            audit=source_audit,
            hash=before,
            proposals=proposals,
            foreign_history=foreign_history,
        )

    def _normalized(self, record):
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
            target = self._normalized(source)
            action = "add" if current is None else "update"
            preserve = False
            if mode == "merge" and loaded["hash"] in seen_packages:
                action = "skip"
            elif current and content_hash(current) == content_hash(target):
                action = "skip"
            elif current and mode == "merge":
                local_chain = [
                    row[0]
                    for row in self.store.db.execute(
                        "SELECT hash FROM revisions WHERE record_id=? ORDER BY revision", (key,)
                    )
                ]
                remote_chain = [row["hash"] for row in loaded["histories"][key]]
                if remote_chain[-1] in local_chain:
                    action = "skip"
                elif local_chain[-1] in remote_chain:
                    preserve = local_chain == remote_chain[: len(local_chain)]
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

    async def plan(
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
        before = database_fingerprint(self.store)
        loaded = await self._load(
            source,
            passphrase=passphrase,
            recovery_code=recovery_code,
            importer=importer,
            approve=approve_proposals,
        )
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
            downgrade_count=sum(r["downgrade"] for r in rows),
            reimport=bool(importer and uid),
        )
        atomic_write(self.plan_path(plan["id"]), canonical(plan).encode())
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
        raw = snapshot(self.store, self.home, self.cid)
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
        atomic_write(
            path, canonical(dict(header=header, nonce=b64(nonce), data=b64(encrypted))).encode()
        )
        saved = json.loads(path.read_bytes())
        checked = AESGCM(key).decrypt(
            un64(saved["nonce"]), un64(saved["data"]), canonical(saved["header"]).encode()
        )
        if checked != raw:
            raise ValueError("导入前快照校验失败")
        db = open_database(checked, self.home, self.cid)
        db.close()
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
        plan = json.loads(self.plan_path(plan_id).read_bytes())
        if plan["companion_id"] != self.cid or plan["id"] != plan_id:
            raise ValueError("计划目标不匹配")
        with runtime_lock(self.home, self.cid, "memory_import"):
            if self.store.transient and self.store.path.exists():
                raise ValueError("计划失效，记忆库已建立")
            if (
                plan["database"] != database_fingerprint(self.store)
                or plan["configuration"] != self.binding_fingerprint()
            ):
                raise ValueError("计划失效，请重新生成")
            if plan["kind"] == "undo":
                return self._apply_undo(plan)
            loaded = await self._load(
                plan["source"],
                passphrase=passphrase,
                recovery_code=recovery_code,
                importer=plan["importer"],
                approve=plan["approve_proposals"],
            )
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
            self.store.materialize()
            backup = self._snapshot()
            import_id, changes = uuid4().hex, {}
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
                    if preserve:
                        for revision in loaded["histories"].get(record_id, []):
                            if previous and revision["revision"] <= previous.revision:
                                continue
                            self.store._save(
                                MemoryRecord.model_validate_json(revision["data"]),
                                operator="import",
                                import_id=import_id,
                            )
                        current = self.store.get_raw(record_id)
                        if current and content_hash(current) != content_hash(target):
                            target = target.model_copy(update={"revision": current.revision + 1})
                            self.store._save(target, operator="import", import_id=import_id)
                    else:
                        target = target.model_copy(
                            update={"revision": previous.revision + 1 if previous else 1}
                        )
                        self.store._save(target, operator="import", import_id=import_id)
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
                        "before": previous.model_dump(mode="json") if previous else None,
                        "after": self.store.get_raw(record_id).model_dump(mode="json"),
                    }
                missing = set(self.store.meta("missing_origins", []))
                missing.update(
                    key
                    for key, r in loaded["records"].items()
                    if r.origin_conversation_id
                    and r.origin_conversation_id not in self.store.instance.get("conversations", {})
                )
                self.store.set_meta("missing_origins", sorted(missing))
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
            self._retention()
            return import_id

    def _record_import(self, import_id, changes, **metadata):
        value = dict(at=self.store.clock.now().isoformat(), changes=changes, **metadata)
        self.store.db.execute("INSERT INTO imports VALUES(?,?)", (import_id, canonical(value)))
        self.store.audit("memory_import", import_id=import_id, changed=len(changes))

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
            conflict = current.model_dump(mode="json") != change["after"]
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
            downgrade_count=sum(r["downgrade"] for r in rows),
        )
        atomic_write(self.plan_path(plan["id"]), canonical(plan).encode())
        preview = dict(plan)
        if show_content:
            preview["content"] = {
                key: {"package": r.content, "current": self.store.get_raw(key).content}
                for key, r in targets.items()
            }
        return preview

    def _apply_undo(self, plan):
        _, rows, targets = self._undo_data(plan["undo"], plan["choices"])
        if rows != plan["records"] or any(
            r["action"] == "conflict" and r["choice"] is None for r in rows
        ):
            raise ValueError("撤销计划失效或冲突尚未选择")
        backup = self._snapshot()
        import_id, changes = uuid4().hex, {}
        with self.store.transaction():
            for row in rows:
                if row["action"] == "conflict" and row["choice"] == "current":
                    continue
                key = row["record_id"]
                current = self.store.get_raw(key)
                target = targets[key].model_copy(update={"revision": current.revision + 1})
                self.store._save(target, operator="undo_import", import_id=import_id)
                changes[key] = {
                    "before": current.model_dump(mode="json"),
                    "after": target.model_dump(mode="json"),
                }
            self._record_import(import_id, changes, undo=plan["undo"], snapshot=backup.name)
        self._retention()
        return import_id
