"""Persistent old-instance inventory and explicit, verified per-file cleanup."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from uuid import uuid4

from clytie.plugin_host.locking import file_lock, process_identity
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical, identifier

from .container import _name, _safe
from .restore import check_source, digest, identity, machine_id, read_state


class OldInstanceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def startup_gate(home):
    """Hold across acquisition of business/supervisor locks, or all cleanup steps."""
    home = Path(home)
    _safe(home.parent)
    name = str(home.resolve())
    if os.name == "nt":
        name = name.casefold()
    path = home.parent / (".clytie-startup-" + hashlib.sha256(name.encode()).hexdigest() + ".lock")
    return file_lock(path, {**process_identity(), "operation": "instance_startup_or_cleanup"})


def _file(path):
    _safe(path)
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise OldInstanceError("unsafe_file")
    hashed = digest(path)
    after = path.stat()

    def fields(value):
        return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns

    if fields(before) != fields(after):
        raise OldInstanceError("source_changed")
    return dict(
        identity=dict(device=before.st_dev, inode=before.st_ino),
        size=before.st_size,
        mtime_ns=before.st_mtime_ns,
        sha256=hashed,
    )


def _snapshot(root):
    _safe(root)
    if not root.is_dir():
        raise OldInstanceError("source_unavailable")
    files, directories = {}, {}
    for path in sorted(root.rglob("*")):
        _safe(path)
        name = path.relative_to(root).as_posix()
        _name(name, {})
        if path.is_dir():
            directories[name] = identity(path)
        else:
            files[name] = _file(path)
    return dict(root_identity=identity(root), files=files, directories=directories)


class OldInstances:
    def __init__(self, home, clock, *, references=None):
        self.home, self.clock = Path(home).resolve(), clock
        _safe(self.home)
        self.home.mkdir(parents=True, exist_ok=True)
        self.registry_path = self.home / "supervisor/old-instances.json"
        self.plans_path = self.home / "supervisor/old-instance-cleanup"
        self.references = references

    def _authority(self):
        return dict(home=str(self.home), identity=identity(self.home))

    def _lock(self):
        return file_lock(
            self.home / "run/old-instances.lock",
            {**process_identity(), "operation": "old_instances"},
        )

    def _read(self):
        _safe(self.registry_path)
        if not self.registry_path.exists():
            return dict(schema_version=1, records=[])
        try:
            value = json.loads(self.registry_path.read_bytes())
            if value["schema_version"] != 1 or not isinstance(value["records"], list):
                raise OldInstanceError("registry_invalid")
            return value
        except (KeyError, TypeError, ValueError) as exc:
            raise OldInstanceError("registry_invalid") from exc

    def _save(self, value):
        _safe(self.registry_path)
        atomic_write(self.registry_path, canonical(value))

    def _record(self, old_id):
        return next((r for r in self._read()["records"] if r["id"] == old_id), None)

    def register(
        self, registration, *, source_instance_id, target_instance_id, operation_id, companions=()
    ):
        """Register a known source; foreign sources remain display-only."""
        if not all(
            isinstance(value, str) and value
            for value in (source_instance_id, target_instance_id, operation_id)
        ) or not isinstance(registration, dict):
            raise OldInstanceError("registration_invalid")
        registration = json.loads(json.dumps(registration))
        with self._lock():
            registry = self._read()
            prior = next(
                (
                    r
                    for r in registry["records"]
                    if r["operation_id"] == operation_id
                    or any(t["operation_id"] == operation_id for t in r.get("transitions", []))
                ),
                None,
            )
            if prior:
                transition = next(
                    (t for t in prior.get("transitions", []) if t["operation_id"] == operation_id),
                    prior,
                )
                if (
                    prior["source_instance_id"] != source_instance_id
                    or transition["target_instance_id"] != target_instance_id
                    or prior["registration"] != registration
                ):
                    raise OldInstanceError("registration_conflict")
                return prior
            local = registration.get("machine_id") == machine_id()
            if local:
                path = Path(registration["home"])
                _safe(path)
                if not path.is_absolute() or str(path.resolve()) != registration["home"]:
                    raise OldInstanceError("source_identity_mismatch")
                try:
                    if identity(path) != registration["identity"]:
                        raise OldInstanceError("source_identity_mismatch")
                except FileNotFoundError:
                    # A confirmed inaccessible source remains visible, but grants
                    # no local inspection/deletion authority until reaccepted.
                    local = False

            transition = dict(
                operation_id=operation_id,
                target_instance_id=target_instance_id,
                registered_at=self.clock.now().isoformat(),
            )
            existing = next(
                (
                    r
                    for r in registry["records"]
                    if r["source_instance_id"] == source_instance_id
                    and r["registration"] == registration
                    and r["state"] != "cleaned"
                ),
                None,
            )
            if existing:
                existing.setdefault(
                    "transitions",
                    [
                        dict(
                            operation_id=existing["operation_id"],
                            target_instance_id=existing["target_instance_id"],
                            registered_at=existing["registered_at"],
                        )
                    ],
                ).append(transition)
                existing["authority"] = self._authority() if local else None
                existing["latest_target_instance_id"] = target_instance_id
                self._save(registry)
                return existing
            record = dict(
                id=uuid4().hex,
                source_instance_id=source_instance_id,
                target_instance_id=target_instance_id,
                operation_id=operation_id,
                registration=registration,
                companions=list(companions),
                authority=self._authority() if local else None,
                registered_at=self.clock.now().isoformat(),
                state="registered",
                audit=[],
                transitions=[transition],
            )
            registry["records"].append(record)
            self._save(registry)
            return record

    def accept_local(self, old_id, *, registration):
        """Explicit user-authorized local acceptance, never inferred during status/plan."""
        with self._lock():
            registry = self._read()
            record = next((r for r in registry["records"] if r["id"] == old_id), None)
            if (
                not record
                or record["registration"] != registration
                or registration.get("machine_id") != machine_id()
            ):
                raise OldInstanceError("source_identity_mismatch")
            path = Path(registration["home"])
            if (
                identity(path) != registration["identity"]
                or str(path.resolve()) != registration["home"]
            ):
                raise OldInstanceError("source_identity_mismatch")
            record["authority"] = self._authority()
            record["audit"].append(
                dict(id=uuid4().hex, kind="accepted_local", at=self.clock.now().isoformat())
            )
            self._save(registry)
            return record

    def _local(self, record):
        return (
            record.get("authority") == self._authority()
            and record["registration"].get("machine_id") == machine_id()
        )

    def status(self, *, warn_count=3, warn_bytes=20 * 1024**3):
        rows, known, unknown, count = [], 0, 0, 0
        for record in self._read()["records"]:
            item = dict(record)
            if record["state"] == "cleaned":
                item.update(bytes=0, observation="cleaned")
            else:
                count += 1
                try:
                    if not self._local(record):
                        raise OldInstanceError("source_unverified")
                    root = Path(record["registration"]["home"])
                    _safe(root)
                    if identity(root) != record["registration"]["identity"]:
                        raise OldInstanceError("source_identity_mismatch")
                    size = 0
                    for path in root.rglob("*"):
                        _safe(path)
                        info = path.stat()
                        if stat.S_ISREG(info.st_mode):
                            size += info.st_size
                    known += size
                    item.update(
                        bytes=size, observation="verified", scanned_at=self.clock.now().isoformat()
                    )
                except (OSError, ValueError, KeyError, TypeError):
                    unknown += 1
                    item.update(bytes=None, observation="unknown")
            rows.append(item)
        return dict(
            count=count,
            known_bytes=known,
            unknown=unknown,
            instances=rows,
            warning=count >= warn_count or known > warn_bytes,
            scanned_at=self.clock.now().isoformat(),
        )

    def register_switch(self, state):
        """Idempotently bind a verified switch to its source; never infer a path."""
        if not state.get("switched") or not state.get("source_stopped"):
            raise OldInstanceError("restore_recovery_blocked")
        registration = state.get("source_registration")
        if registration is None:
            # Legacy packages without a verifiable source location grant no authority.
            return None
        return self.register(
            registration,
            source_instance_id=state["source_instance_id"],
            target_instance_id=state["target_instance_id"],
            operation_id=state["operation_id"],
            companions=state["companions"],
        )

    def _references(self):
        paths = list(self.references()) if self.references else []
        directory = self.home / "supervisor/restore-plans"
        if directory.exists():
            _safe(directory)
            for path in sorted(directory.glob("*.json")):
                _safe(path)
                plan = json.loads(path.read_bytes())
                target = Path(plan["target"])
                try:
                    state = read_state(target) if target.exists() else None
                except (OSError, ValueError):
                    state = None
                if not state or not state.get("switched"):
                    paths += [Path(plan["source"]), target]
        return [Path(p).resolve() for p in paths]

    def _protection(self, record, *, retirement=None):
        if not self._local(record):
            return [dict(code="source_unverified")]
        root = Path(record["registration"]["home"])
        reasons = []
        if self.home.is_relative_to(root) or root.is_relative_to(self.home):
            reasons.append(dict(code="current_instance"))
        try:
            if identity(root) != record["registration"]["identity"]:
                reasons.append(dict(code="source_identity_mismatch"))
            source_state = dict(
                source_registration=record["registration"], companions=record["companions"]
            )
            # Discover all current companion directories as well as recorded coverage.
            directory = root / "companions"
            if directory.exists():
                _safe(directory)
                known = {c["id"] for c in source_state["companions"]}
                source_state["companions"] = list(source_state["companions"])
                for path in directory.iterdir():
                    _safe(path)
                    if path.is_dir() and path.name not in known:
                        source_state["companions"].append(dict(id=identifier(path.name)))
            status = check_source(source_state)["status"]
            if status != "stopped":
                reasons.append(
                    dict(code="source_active" if status == "active" else "source_unverified")
                )
            if any(p.is_relative_to(root) or root.is_relative_to(p) for p in self._references()):
                reasons.append(dict(code="restore_reference"))
            from .repository import MARKER, REFERENCES, SourceRepository

            repository_root = root / "backups/regular"
            marker = repository_root / MARKER
            if retirement is not None and (repository_root / REFERENCES).exists():
                references = json.loads(atomic_read(repository_root / REFERENCES))
                if (
                    references.get("repository_retirement") != retirement["operation_id"]
                    or references.get("references") != {}
                ):
                    reasons.append(dict(code="restore_reference"))
            elif marker.exists() and retirement is None:
                repository = SourceRepository(repository_root, self.clock)
                references = json.loads(atomic_read(repository.root / REFERENCES))
                if (
                    references.get("schema_version") != 1
                    or not isinstance(references.get("references"), dict)
                    or not isinstance(references.get("previews"), dict)
                ):
                    raise OldInstanceError("source_unverified")
                if references["references"] or references.get("repository_retirement"):
                    reasons.append(dict(code="restore_reference"))
            legacy = root / "supervisor/backup-previews.json"
            if legacy.exists():
                _safe(legacy)
                references = json.loads(atomic_read(legacy))
                if references.get("references") != {}:
                    reasons.append(dict(code="restore_reference"))
        except (OSError, ValueError, KeyError, TypeError):
            reasons.append(dict(code="source_unverified"))
        return reasons

    def _preserved(self, root, name, original, requested):
        candidate = Path(requested) if requested else self.home / name
        _safe(candidate)
        candidate = candidate.resolve()
        for registered in self._read()["records"]:
            if (
                registered["registration"].get("machine_id") == machine_id()
                and registered["state"] != "cleaned"
            ):
                old = Path(registered["registration"]["home"]).resolve()
                if candidate.is_relative_to(old):
                    raise OldInstanceError("preservation_inside_old_instance")
        if candidate.is_relative_to(root):
            raise OldInstanceError("preservation_inside_old_instance")
        copy = _file(candidate)
        if (
            copy["identity"] == original["identity"]
            or copy["size"] != original["size"]
            or copy["sha256"] != original["sha256"]
        ):
            raise OldInstanceError("preservation_unverified")
        return dict(path=str(candidate), **copy)

    def _plan_path(self, plan_id):
        if (
            not isinstance(plan_id, str)
            or len(plan_id) != 32
            or any(c not in "0123456789abcdef" for c in plan_id)
        ):
            raise OldInstanceError("plan_invalid")
        return self.plans_path / (plan_id + ".json")

    def _write_plan(self, plan):
        path = self._plan_path(plan["id"])
        _safe(path)
        atomic_write(path, canonical(plan))

    def plan_cleanup(self, old_id, *, preserved=None):
        preserved = preserved or {}
        with self._lock():
            record = self._record(old_id)
            if not record or record["state"] == "cleaned":
                raise OldInstanceError("source_unavailable")
            blocked = self._protection(record)
            snapshot, entries = None, []
            if self._local(record) and not any(
                reason["code"]
                in {
                    "current_instance",
                    "source_unverified",
                    "source_identity_mismatch",
                    "source_active",
                }
                for reason in blocked
            ):
                root = Path(record["registration"]["home"])
                try:
                    snapshot = _snapshot(root)
                    for name, item in snapshot["files"].items():
                        is_key = name == "secrets/backup.key"
                        entry = dict(path=name, **item, unique=True, key=is_key, preserved=None)
                        try:
                            entry["preserved"] = self._preserved(
                                root, name, item, preserved.get(name)
                            )
                            entry["unique"] = False
                        except (OSError, ValueError):
                            blocked.append(
                                dict(
                                    code="unique_key_unpreserved"
                                    if is_key
                                    else "unique_data_unpreserved",
                                    path=name,
                                )
                            )
                        entries.append(entry)
                except (OSError, ValueError):
                    blocked.append(dict(code="source_unverified"))
            plan = dict(
                id=uuid4().hex,
                old_id=old_id,
                record=record,
                created_at=self.clock.now().isoformat(),
                snapshot=snapshot,
                files=entries,
                blocked=blocked,
                allowed=not blocked,
                state="planned",
                deleted=[],
                removed_directories=[],
                audit=[],
                preservation_evidence="byte_identity_only_not_restore_drill",
            )
            self._write_plan(plan)
            return plan

    def _revalidate(self, plan):
        record = self._record(plan["old_id"])
        if record != plan["record"] or self._protection(
            record, retirement=plan.get("repository_retirement")
        ):
            raise OldInstanceError("protection_changed")
        root = Path(record["registration"]["home"])
        current = _snapshot(root)
        expected = plan["snapshot"]
        remaining_files = {
            name: item for name, item in expected["files"].items() if name not in plan["deleted"]
        }
        remaining_directories = {
            name: item
            for name, item in expected["directories"].items()
            if name not in plan["removed_directories"]
        }
        if current != dict(
            root_identity=expected["root_identity"],
            files=remaining_files,
            directories=remaining_directories,
        ):
            raise OldInstanceError("plan_changed")
        for entry in plan["files"]:
            if entry.get("cleanup_metadata"):
                continue
            preserved = entry["preserved"]
            if self._preserved(root, entry["path"], entry, preserved["path"]) != preserved:
                raise OldInstanceError("preservation_changed")
        return root

    def _retire_repository(self, plan, root):
        from .repository import MARKER, REFERENCES, SourceRepository

        repository_root = root / "backups/regular"
        if not (repository_root / MARKER).exists():
            return
        repository = SourceRepository(repository_root, self.clock)
        # Durable intent forbids new content readers without holding a lock for
        # the entire directory cleanup. A crash deliberately leaves it blocked.
        plan["repository_retirement"] = {
            "root": str(repository_root),
            "repository_id": repository.repository_id,
            "operation_id": plan["id"],
        }
        self._event(plan, "repository_retiring")
        repository.retire(plan["id"])
        for path in (
            repository_root / REFERENCES,
            (repository_root / REFERENCES).with_suffix(".lock"),
        ):
            name = path.relative_to(root).as_posix()
            current = _file(path)
            if name not in plan["snapshot"]["files"]:
                plan["files"].append(dict(path=name, **current, cleanup_metadata=True))
            # Existing preservation receipts still prove the pre-cleanup content;
            # only these two files are changed by our own admission barrier.
            plan["snapshot"]["files"][name] = current
        self._event(plan, "repository_retired")

    def _event(self, plan, kind, **fields):
        plan["audit"].append(
            dict(id=uuid4().hex, kind=kind, at=self.clock.now().isoformat(), **fields)
        )
        self._write_plan(plan)

    def _release_retirement(self, plan):
        """Only called while the stopped cleanup's startup/operation gates are held."""
        from .repository import MARKER, REFERENCES, SourceRepository

        retired = plan.get("repository_retirement")
        if retired is None:
            return
        root = Path(retired["root"])
        if not (root / MARKER).exists() or not (root / REFERENCES).exists():
            return  # Incomplete repository already rejects all new source readers.
        try:
            SourceRepository(root, self.clock, expected_id=retired["repository_id"]).unretire(
                plan["id"]
            )
            self._event(plan, "repository_retirement_released")
        except (OSError, ValueError):
            self._event(plan, "repository_retirement_retained")

    def apply_cleanup(self, plan_id, *, check=None):
        _safe(self._plan_path(plan_id))
        plan = json.loads(self._plan_path(plan_id).read_bytes())
        if plan["state"] == "completed":
            return plan
        if plan["state"] != "planned" or not plan["allowed"]:
            raise OldInstanceError("plan_not_executable")
        root = Path(plan["record"]["registration"]["home"])
        with startup_gate(root), self._lock():
            # A second caller may have completed while waiting for this gate.
            plan = json.loads(self._plan_path(plan_id).read_bytes())
            if plan["state"] != "planned":
                raise OldInstanceError("plan_not_executable")
            plan["state"] = "applying"
            self._event(plan, "started")
            try:
                self._revalidate(plan)
                self._retire_repository(plan, root)
                for entry in plan["files"]:
                    if check:
                        check()
                    self._revalidate(plan)
                    name = entry["path"]
                    self._event(plan, "deleting", path=name)
                    (root / name).unlink()
                    plan["deleted"].append(name)
                    self._event(plan, "deleted", path=name)
                directories = sorted(
                    plan["snapshot"]["directories"],
                    key=lambda name: (name.count("/"), name),
                    reverse=True,
                )
                for name in directories:
                    if check:
                        check()
                    self._revalidate(plan)
                    self._event(plan, "removing_directory", path=name)
                    (root / name).rmdir()
                    plan["removed_directories"].append(name)
                    self._event(plan, "removed_directory", path=name)
                self._revalidate(plan)
                self._event(plan, "removing_root")
                root.rmdir()
                plan["state"] = "completed"
                self._event(plan, "completed")
                registry = self._read()
                record = next(r for r in registry["records"] if r["id"] == plan["old_id"])
                record.update(state="cleaned", cleanup_plan_id=plan_id)
                record["audit"].append(
                    dict(
                        id=uuid4().hex,
                        kind="cleaned",
                        at=self.clock.now().isoformat(),
                        plan_id=plan_id,
                    )
                )
                self._save(registry)
                return plan
            except (OSError, ValueError, KeyError, TypeError) as exc:
                plan["state"] = "stopped"
                plan["reason"] = (
                    exc.code if isinstance(exc, OldInstanceError) else "filesystem_error"
                )
                self._event(plan, "stopped", reason=plan["reason"])
                self._release_retirement(plan)
                return plan

    def operation(self, plan_id):
        path = self._plan_path(plan_id)
        _safe(path)
        return json.loads(path.read_bytes())

    def recover_cleanup(self, plan_id):
        """Reconcile an interrupted journal without repeating any unlink/rmdir."""
        plan = self.operation(plan_id)
        root = Path(plan["record"]["registration"]["home"])
        with startup_gate(root), self._lock():
            plan = self.operation(plan_id)
            if plan["state"] != "applying":
                return plan
            _safe(root)
            attempted = {event["path"] for event in plan["audit"] if event["kind"] == "deleting"}
            unknown = []
            for name in sorted(attempted - set(plan["deleted"])):
                _name(name, {})
                path = root / name
                _safe(path)
                if not path.exists():
                    unknown.append(name)
            if (
                any(event["kind"] == "removing_root" for event in plan["audit"])
                and not root.exists()
            ):
                unknown.append("<root>")
            plan.update(state="stopped", reason="recovery_requires_new_plan", unknown_paths=unknown)
            self._event(plan, "recovered_without_replay", unknown_count=len(unknown))
            self._release_retirement(plan)
            return plan
