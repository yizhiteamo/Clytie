"""Durable recovery preview states and independently owned source references."""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from clytie.plugin_host.locking import OperationBusy, file_lock, process_identity
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical, contained

from .container import _safe


class PreviewError(ValueError):
    def __init__(self, code, *, operation_id=None):
        self.code, self.operation_id = code, operation_id
        super().__init__(code)


def source_key(source):
    _safe(Path(source))
    value = str(Path(source).resolve())
    return value.casefold() if os.name == "nt" else value


class _LocalCatalog:
    """One atomic record makes preview/reference transfers crash consistent.

    Lock order: caller admission/index lock, this catalog lock, operation journal.
    No callback may reacquire the catalog. Unknown/orphan operation references are
    deliberately retained until an owner supplies verified completion evidence.
    Synchronous entrypoints belong in worker threads, never on the API event loop.
    """

    def __init__(self, home, clock):
        self.home, self.clock = Path(home).resolve(), clock
        self.path = contained(self.home, self.home / "supervisor/backup-previews.json")
        self.holder_instance_id = None
        self.repository = None

    @contextmanager
    def _transaction(self):
        manager = file_lock(
            self.path.with_suffix(".lock"), {**process_identity(), "operation": "backup_references"}
        )
        deadline = self.clock.monotonic() + 10
        for attempt in range(1001):
            try:
                manager.__enter__()
                break
            except OperationBusy:
                if self.clock.monotonic() >= deadline or attempt == 1000:
                    raise PreviewError("reference_busy") from None
                threading.Event().wait(0.01)
                manager = file_lock(
                    self.path.with_suffix(".lock"),
                    {**process_identity(), "operation": "backup_references"},
                )
        try:
            if self.repository is not None:
                self.repository.revalidate()
                if not self.path.exists():
                    raise PreviewError("reference_recovery_blocked")
            try:
                data = (
                    json.loads(atomic_read(self.path))
                    if self.path.exists()
                    else {"schema_version": 1, "previews": {}, "references": {}}
                )
                if (
                    data["schema_version"] != 1
                    or not isinstance(data["previews"], dict)
                    or not isinstance(data["references"], dict)
                ):
                    raise ValueError()
                for key, ref in data["references"].items():
                    if ref["owner_id"] != key or not ref["source"] or not ref["kind"]:
                        raise ValueError()
                    if not ref["executor"]["started_at"] or not ref["executor"]["pid"]:
                        raise ValueError()
                    if self.repository is not None and (
                        ref.get("source_repository_id") != self.repository.repository_id
                        or not ref.get("holder_instance_id")
                        or key != f"{ref['holder_instance_id']}:{ref.get('holder_operation_id')}"
                        or not isinstance(ref.get("source_identity"), dict)
                        or not ref.get("backup_id")
                    ):
                        raise ValueError()
                for key, plan in data["previews"].items():
                    if (
                        plan["plan_id"] != key
                        or plan["kind"] not in {"instance", "memory"}
                        or plan["state"]
                        not in {"ready", "expired", "cancelled", "stale", "consumed"}
                        or datetime.fromisoformat(plan["expires_at"]).tzinfo is None
                    ):
                        raise ValueError()
                    if plan["state"] == "ready":
                        ref = data["references"][plan["reference_id"]]
                        if ref["kind"] != "preview" or ref["source"] != plan["source"]:
                            raise ValueError()
                        if self.repository is not None and (
                            ref["holder_instance_id"] != plan.get("holder_instance_id")
                        ):
                            raise ValueError()
                self._expire(data)
            except (ValueError, KeyError, TypeError):
                raise PreviewError("reference_recovery_blocked") from None
            # Expiry must survive a subsequent rejected request, including clock rollback.
            atomic_write(self.path, canonical(data), retry_sharing=True)
            try:
                yield data
            finally:
                atomic_write(self.path, canonical(data), retry_sharing=True)
        finally:
            manager.__exit__(None, None, None)

    def _expire(self, data):
        now = self.clock.now().astimezone(timezone.utc)
        for plan in data["previews"].values():
            if plan["state"] == "ready" and now >= datetime.fromisoformat(plan["expires_at"]):
                self._transition(plan, "expired")
                data["references"].pop(plan["reference_id"], None)

    def _transition(self, plan, state):
        plan["state"] = state
        plan.setdefault("history", []).append(
            dict(
                state=state,
                at=self.clock.now().astimezone(timezone.utc).isoformat(),
                operation_id=plan.get("operation_id"),
            )
        )

    def _get(self, data, kind, plan_id):
        plan = data["previews"].get(plan_id)
        if kind not in {"instance", "memory"} or not plan or plan["kind"] != kind:
            raise PreviewError("plan_not_found")
        if self.holder_instance_id and plan.get("holder_instance_id") != self.holder_instance_id:
            raise PreviewError("plan_not_found")
        return plan

    def _ready(self, plan):
        if plan["state"] != "ready":
            raise PreviewError(
                "plan_" + ("in_use" if plan["state"] == "consumed" else plan["state"]),
                operation_id=plan.get("operation_id"),
            )

    def _refkey(self, owner_id):
        return f"{self.holder_instance_id}:{owner_id}" if self.holder_instance_id else owner_id

    def _reference(self, data, source, owner_id, kind):
        from .repository import physical_identity

        if data.get("repository_retirement"):
            raise PreviewError("source_repository_retiring")
        operation = owner_id
        owner_id = self._refkey(owner_id)
        key = source_key(source)
        previous = data["references"].get(owner_id)
        if previous:
            if previous["source"] != key or previous["kind"] != kind:
                raise PreviewError("reference_mismatch")
            if previous.get("source_identity") != physical_identity(source):
                raise PreviewError("source_repository_identity_mismatch")
            return
        if self.repository is not None:
            rows = self.repository.index()["records"]
            ids = [
                key for key, row in rows.items() if source_key(row["path"]) == source_key(source)
            ]
            if len(ids) != 1:
                raise PreviewError("source_repository_ownership_mismatch")
            self.repository.resolve(ids[0], source=source)
        if not Path(source).is_file():
            raise PreviewError("source_missing")
        data["references"][owner_id] = dict(
            source=key,
            owner_id=owner_id,
            kind=kind,
            executor=process_identity(),
            created_at=self.clock.now().isoformat(),
            recovery_required=False,
            holder_instance_id=self.holder_instance_id,
            holder_operation_id=operation,
            source_identity=physical_identity(source),
            source_repository_id=self.repository.repository_id if self.repository else None,
            backup_id=ids[0] if self.repository else None,
        )

    def reference(self, source, owner_id, *, kind="reader"):
        with self._transaction() as data:
            self._reference(data, source, owner_id, kind)
        return owner_id

    def release(self, owner_id, *, recovery_required=False):
        """Caller must have finished its reader/worker and checked recovery needs."""
        with self._transaction() as data:
            ref = data["references"].get(self._refkey(owner_id))
            if ref and recovery_required:
                ref["recovery_required"] = True
            elif ref:
                del data["references"][self._refkey(owner_id)]

    @contextmanager
    def protect(self, source, owner_id, *, kind="reader"):
        self.reference(source, owner_id, kind=kind)
        yield owner_id
        self.release(owner_id)

    def publish(self, kind, plan_id, source, generation_id, *, target, fingerprint):
        if kind not in {"instance", "memory"}:
            raise PreviewError("plan_not_found")
        with self._transaction() as data:
            if plan_id in data["previews"]:
                prior = self._get(data, kind, plan_id)
                if (
                    prior["source"] != source_key(source)
                    or prior["target"] != target
                    or prior["fingerprint"] != fingerprint
                ):
                    raise PreviewError("plan_stale")
                return dict(prior)
            generation = data["references"].get(self._refkey(generation_id))
            if (
                not generation
                or generation["source"] != source_key(source)
                or generation["kind"] != "generation"
            ):
                raise PreviewError("reference_missing")
            now = self.clock.now().astimezone(timezone.utc)
            ref = "preview:" + plan_id
            self._reference(data, source, ref, "preview")
            plan = dict(
                kind=kind,
                plan_id=plan_id,
                source=source_key(source),
                target=target,
                fingerprint=fingerprint,
                state="ready",
                created_at=now.isoformat(),
                expires_at=(now + timedelta(hours=24)).isoformat(),
                operation_id=None,
                reference_id=self._refkey(ref),
                holder_instance_id=self.holder_instance_id,
            )
            data["previews"][plan_id] = plan
            self._transition(plan, "ready")
            del data["references"][self._refkey(generation_id)]
            return dict(plan)

    def get(self, kind, plan_id):
        with self._transaction() as data:
            return dict(self._get(data, kind, plan_id))

    def for_operation(self, operation_id):
        """Find durable execution bindings even if operation evidence was interrupted."""
        with self._transaction() as data:
            return [
                json.loads(json.dumps(plan))
                for plan in data["previews"].values()
                if plan["state"] == "consumed"
                and plan["operation_id"] == operation_id
                and (
                    not self.holder_instance_id
                    or plan.get("holder_instance_id") == self.holder_instance_id
                )
            ]

    def cancel(self, kind, plan_id):
        with self._transaction() as data:
            plan = self._get(data, kind, plan_id)
            if plan["state"] == "consumed":
                raise PreviewError("plan_in_use", operation_id=plan["operation_id"])
            if plan["state"] == "ready":
                self._transition(plan, "cancelled")
                data["references"].pop(plan["reference_id"], None)
            return dict(plan)

    def _validate(self, data, plan, validate):
        if validate is not None:
            try:
                validate()
            except (ValueError, OSError):
                self._transition(plan, "stale")
                data["references"].pop(plan["reference_id"], None)
                raise

    def accept(self, kind, plan_id, operation_id, *, validate=None):
        with self._transaction() as data:
            plan = self._get(data, kind, plan_id)
            if plan["state"] == "consumed":
                return plan["operation_id"], False
            self._ready(plan)
            self._validate(data, plan, validate)
            self._reference(data, plan["source"], operation_id, "execution")
            plan["operation_id"] = operation_id
            self._transition(plan, "consumed")
            data["references"].pop(plan["reference_id"], None)
            return operation_id, True

    @contextmanager
    def reading(self, kind, plan_id, *, validate=None):
        owner_id = "reader:" + uuid4().hex
        with self._transaction() as data:
            plan = self._get(data, kind, plan_id)
            self._ready(plan)
            self._reference(data, plan["source"], owner_id, "reader")
            try:
                self._validate(data, plan, validate)
            except BaseException:
                data["references"].pop(self._refkey(owner_id), None)
                raise
        try:
            yield owner_id
        finally:
            self.release(owner_id)

    def holders(self, source):
        with self._transaction() as data:
            key = source_key(source)
            result = [dict(ref) for ref in data["references"].values() if ref["source"] == key]
        for ref in result:
            try:
                actual = process_identity(ref["executor"]["pid"])
                state = "active" if actual == ref["executor"] else "exited"
            except (ProcessLookupError, FileNotFoundError):
                state = "exited"
            except OSError:
                state = "unknown"
            ref["executor_state"] = state
            ref["reason"] = (
                "preview"
                if ref["kind"] == "preview"
                else (
                    "active_reference"
                    if state == "active" and not ref["recovery_required"]
                    else "recovery_unverified"
                )
            )
        return result

    @contextmanager
    def deletion(self, source):
        with self._transaction() as data:
            key = source_key(source)
            yield not any(ref["source"] == key for ref in data["references"].values())


class PreviewCatalog(_LocalCatalog):
    """Local recovery plans with explicitly bound source-owned protection receipts."""

    @contextmanager
    def _routes(self):
        path = self.home / "supervisor/backup-source-bindings.json"
        from .repository import repository_lock

        with repository_lock(path.with_suffix(".lock"), "source_bindings"):
            try:
                data = (
                    json.loads(atomic_read(path))
                    if path.exists()
                    else {
                        "schema_version": 1,
                        "sources": {},
                        "plans": {},
                        "owners": {},
                        "pending_release": {},
                    }
                )
                if data["schema_version"] != 1:
                    raise ValueError()
                yield data
            finally:
                if "data" in locals():
                    atomic_write(path, canonical(data), retry_sharing=True)

    def _holder(self):
        from .repository import holder_identity

        self.home.mkdir(parents=True, exist_ok=True)
        return holder_identity(self.home)

    def bind_source(self, source, source_repository, *, confirmed=False):
        from .repository import SourceRepository

        repository = SourceRepository(source_repository["root"], self.clock)
        binding = repository.binding(source_repository, confirmed=confirmed, source=source)
        binding["holder_instance_id"] = self._holder()
        with self._routes() as data:
            previous = data["sources"].get(source_key(source))
            if previous and previous != binding:
                raise PreviewError("source_repository_identity_mismatch")
            data["sources"][source_key(source)] = binding
        return dict(binding)

    def _source_binding(self, source):
        with self._routes() as data:
            binding = data["sources"].get(source_key(source))
        if binding:
            return binding
        # Only this management home's explicitly owned regular repository is implicit.
        root = self.home / "backups/regular"
        if Path(source).resolve().parent == root:
            from .repository import MARKER, SourceRepository

            if (root / MARKER).exists():
                repository = SourceRepository(root, self.clock)
                rows = repository.index()["records"]
                ids = [
                    key
                    for key, row in rows.items()
                    if source_key(row["path"]) == source_key(source)
                ]
                if len(ids) != 1:
                    raise PreviewError("source_repository_ownership_mismatch")
                return self.bind_source(
                    source,
                    {
                        "root": str(root),
                        "repository_id": repository.repository_id,
                        "backup_id": ids[0],
                    },
                    confirmed=True,
                )
        return None

    def _authority(self, binding, *, verify_source=False):
        from .repository import SourceRepository

        repo = SourceRepository(binding["root"], self.clock, expected_id=binding["repository_id"])
        if not repo.marker.get("migration_complete"):
            raise PreviewError("reference_recovery_blocked")
        if repo.identity(repo.root) != binding["root_identity"]:
            raise PreviewError("source_repository_identity_mismatch")
        if binding["holder_instance_id"] != self._holder():
            raise PreviewError("holder_identity_mismatch")
        if verify_source:
            actual = repo.resolve(binding["backup_id"], source=binding["source"])
            if actual["source_identity"] != binding["source_identity"]:
                raise PreviewError("source_repository_identity_mismatch")
        return repo.catalog(binding["holder_instance_id"])

    def _plan_binding(self, kind, plan_id):
        with self._routes() as data:
            row = data["plans"].get(plan_id)
            if row and row["kind"] != kind:
                raise PreviewError("plan_not_found")
            return json.loads(json.dumps(row)) if row else None

    def reference(self, source, owner_id, *, kind="reader"):
        binding = self._source_binding(source)
        if not binding:
            return super().reference(source, owner_id, kind=kind)
        # Persist intent before the source write. An interrupted reply is reconciled
        # with the same holder and owner ID, never a replacement registration.
        with self._routes() as data:
            old = data["owners"].get(owner_id)
            if old and old != binding:
                raise PreviewError("reference_mismatch")
            data["owners"][owner_id] = binding
        authority = self._authority(binding, verify_source=True)
        return authority.reference(source, owner_id, kind=kind)

    def release(self, owner_id, *, recovery_required=False):
        with self._routes() as data:
            binding = data["owners"].get(owner_id)
        if not binding:
            return super().release(owner_id, recovery_required=recovery_required)
        try:
            self._authority(binding).release(owner_id, recovery_required=recovery_required)
        except (OSError, ValueError):
            with self._routes() as data:
                data["pending_release"][owner_id] = {
                    "binding": binding,
                    "recovery_required": recovery_required,
                    "state": "pending",
                }
            return False
        with self._routes() as data:
            data["pending_release"].pop(owner_id, None)
            if not recovery_required:
                data["owners"].pop(owner_id, None)
        return True

    def publish(self, kind, plan_id, source, generation_id, *, target, fingerprint):
        binding = self._source_binding(source)
        if not binding:
            return super().publish(
                kind, plan_id, source, generation_id, target=target, fingerprint=fingerprint
            )
        local = {
            "kind": kind,
            "source_repository": binding,
            "target": target,
            "fingerprint": fingerprint,
            "phase": "publishing",
            "generation_id": generation_id,
        }
        with self._routes() as data:
            prior = data["plans"].get(plan_id)
            if prior and any(
                prior[key] != local[key]
                for key in ("kind", "source_repository", "target", "fingerprint")
            ):
                raise PreviewError("plan_stale")
            if not prior:
                data["plans"][plan_id] = local
        import hashlib

        authority = self._authority(binding)
        authority.publish(
            kind,
            plan_id,
            source,
            generation_id,
            target=hashlib.sha256(canonical(target)).hexdigest(),
            fingerprint=hashlib.sha256(canonical(fingerprint)).hexdigest(),
        )
        with self._routes() as data:
            data["plans"][plan_id]["phase"] = "published"
        return self.get(kind, plan_id)

    def get(self, kind, plan_id):
        local = self._plan_binding(kind, plan_id)
        if not local:
            return super().get(kind, plan_id)
        plan = self._authority(local["source_repository"]).get(kind, plan_id)
        return {
            **plan,
            "target": local["target"],
            "fingerprint": local["fingerprint"],
            "source_repository": local["source_repository"],
        }

    def cancel(self, kind, plan_id):
        local = self._plan_binding(kind, plan_id)
        if not local:
            return super().cancel(kind, plan_id)
        self._authority(local["source_repository"]).cancel(kind, plan_id)
        return self.get(kind, plan_id)

    def accept(self, kind, plan_id, operation_id, *, validate=None):
        local = self._plan_binding(kind, plan_id)
        if not local:
            return super().accept(kind, plan_id, operation_id, validate=validate)
        binding = local["source_repository"]
        with self._routes() as data:
            data["owners"][operation_id] = binding
            data["plans"][plan_id]["phase"] = "accepting"
        authority = self._authority(binding)

        def verify_binding():
            # Called under the source transaction after the terminal/idempotent
            # state check, and before any caller content validation.
            self._authority(binding, verify_source=True)
            if validate is not None:
                validate()

        accepted, fresh = authority.accept(kind, plan_id, operation_id, validate=verify_binding)
        with self._routes() as data:
            data["owners"][accepted] = binding
            data["plans"][plan_id].update(phase="accepted", operation_id=accepted)
        return accepted, fresh

    @contextmanager
    def reading(self, kind, plan_id, *, validate=None):
        local = self._plan_binding(kind, plan_id)
        if not local:
            with super().reading(kind, plan_id, validate=validate) as owner:
                yield owner
            return
        binding = local["source_repository"]
        # The authority acquires an independent reader under the preview lock.
        # A killed reader remains registered; deadline processing cannot release it.
        with self._authority(binding, verify_source=True).reading(
            kind, plan_id, validate=validate
        ) as owner:
            yield owner

    def for_operation(self, operation_id):
        result = super().for_operation(operation_id)
        with self._routes() as data:
            plans = [(key, row["kind"]) for key, row in data["plans"].items()]
        for plan_id, kind in plans:
            plan = self.get(kind, plan_id)
            if plan["state"] == "consumed" and plan["operation_id"] == operation_id:
                result.append(plan)
        return result

    def holders(self, source):
        binding = self._source_binding(source)
        if binding:
            return self._authority(binding).holders(source)
        return super().holders(source)

    def status(self):
        """Authenticated metadata: unknown authority is never reported as zero."""
        from .repository import MARKER, SourceRepository

        try:
            with self._routes() as data:
                bindings = list(data["sources"].values())
                pending = [
                    {
                        "operation_id": key,
                        "state": "pending",
                        "repository_id": row["binding"]["repository_id"],
                    }
                    for key, row in data["pending_release"].items()
                ]
        except (OSError, ValueError, KeyError, TypeError):
            return {
                "state": "unavailable",
                "code": "reference_recovery_blocked",
                "repositories": None,
                "pending_release": None,
                "legacy_references": None,
            }
        roots = {row["root"]: row["repository_id"] for row in bindings}
        own = self.home / "backups/regular"
        if (own / MARKER).exists():
            roots.setdefault(str(own), None)
        repositories = []
        for root, expected in roots.items():
            try:
                repository = SourceRepository(root, self.clock, expected_id=expected)
                authority = repository.catalog(None)
                with authority._transaction() as data:
                    sources = {ref["source"] for ref in data["references"].values()}
                refs = [ref for source in sources for ref in authority.holders(source)]
                if Path(root) != own:
                    holder = self._holder()
                    refs = [ref for ref in refs if ref.get("holder_instance_id") == holder]
                repositories.append(
                    {
                        "repository_id": repository.repository_id,
                        "state": "ready"
                        if repository.marker.get("migration_complete")
                        else "migration_blocked",
                        "references": [
                            {
                                key: ref.get(key)
                                for key in (
                                    "holder_instance_id",
                                    "holder_operation_id",
                                    "backup_id",
                                    "kind",
                                    "created_at",
                                    "recovery_required",
                                    "executor_state",
                                    "reason",
                                )
                            }
                            for ref in refs
                        ],
                    }
                )
            except (OSError, ValueError) as exc:
                repositories.append(
                    {
                        "repository_id": expected,
                        "state": "unavailable",
                        "code": getattr(exc, "code", "reference_recovery_blocked"),
                        "references": None,
                    }
                )
        legacy = []
        if self.path.exists():
            try:
                migrated = {row["source"] for row in bindings}
                with super()._transaction() as data:
                    legacy = [
                        {
                            key: ref.get(key)
                            for key in ("owner_id", "kind", "created_at", "recovery_required")
                        }
                        for ref in data["references"].values()
                        if ref["source"] not in migrated
                    ]
            except (OSError, ValueError):
                legacy = None
        return {
            "repositories": repositories,
            "pending_release": pending,
            "legacy_references": legacy,
        }

    @contextmanager
    def deletion(self, source):
        binding = self._source_binding(source)
        if binding:
            from .repository import SourceRepository

            repo = SourceRepository(
                binding["root"], self.clock, expected_id=binding["repository_id"]
            )
            with repo.deletion(source) as allowed:
                yield allowed
        else:
            with super().deletion(source) as allowed:
                yield allowed
