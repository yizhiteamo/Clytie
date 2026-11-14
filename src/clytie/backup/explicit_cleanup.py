"""Fixed explicit selections in the current home's owned regular repository."""

from collections import defaultdict
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import yaml

from clytie.supervisor.common import write

from .storage import BackupStorage


def _verified(store, row):
    return store.verified(row)


def _minimum(store, rows):
    groups = defaultdict(list)
    for row in rows:
        group = groups[(row["scope"], row["group"])]
        if row["completeness"] == "complete" and _verified(store, row):
            group.append(row)
    protected = set()
    for group in groups.values():
        group.sort(key=lambda r: (datetime.fromisoformat(r["completed_at"]), r["backup_id"]))
        protected.update(r["backup_id"] for r in group[-3:])
    return protected, groups


def _layers(store, rows):
    """Display the same automatic calendar bucket winners, without authorizing deletion."""
    from .retention import RetentionConfig, calendar_bucket

    path = store.home / "clytie.local.yaml"
    source = (yaml.safe_load(path.read_bytes()) or {}) if path.exists() else {}
    settings = source.get("backup", {})
    config = dict(settings.get("retention", {}))
    target = config.pop("target_gib", 20)
    config["target_bytes"] = None if target is None else int(target * 1024**3)
    policy = RetentionConfig(**config)
    now = store.retention_now()
    layers = {row["backup_id"]: (["manual"] if row["manual"] else []) for row in rows}
    groups = defaultdict(list)
    for row in rows:
        if not row["manual"] and row["completeness"] == "complete" and _verified(store, row):
            groups[(row["scope"], row["group"])].append(row)
    for (scope, _), group in groups.items():
        group.sort(key=lambda r: (datetime.fromisoformat(r["completed_at"]), r["backup_id"]))
        if scope == "memory":
            for row in group[-policy.recent_memory :]:
                layers[row["backup_id"]].append("recent")
        buckets = {}
        for row in group:
            bucket = calendar_bucket(datetime.fromisoformat(row["completed_at"]), now, policy)
            if bucket is None:
                continue
            buckets[bucket] = row["backup_id"]
        for bucket, key in buckets.items():
            layers[key].append({"day": "daily", "week": "weekly", "month": "monthly"}[bucket[0]])
    return layers


def plan(service, backup_ids, plan_id=None):
    if (
        not isinstance(backup_ids, list)
        or not backup_ids
        or any(
            not isinstance(key, str)
            or not key
            or len(key) > 128
            or any(not (char.isascii() and (char.isalnum() or char in "_-")) for char in key)
            for key in backup_ids
        )
        or len(set(backup_ids)) != len(backup_ids)
    ):
        raise ValueError("cleanup_selection_invalid")
    store = BackupStorage(service.home, service.clock)
    with store.lock():
        rows = store.list()
        minimum, groups = _minimum(store, rows)
        layers = _layers(store, rows)
        records = {row["backup_id"]: row for row in rows}
        items = []
        for key in backup_ids:
            row = records.get(key)
            reason = None
            if row is None:
                reason = "backup_not_owned"
            elif not _verified(store, row):
                reason = "backup_evidence_mismatch"
            elif key in minimum:
                reason = "minimum_restore_points"
            elif store.repository.catalog(None).holders(row["path"]):
                reason = "backup_referenced"
            item = (
                {
                    field: row[field]
                    for field in (
                        "scope",
                        "group",
                        "completed_at",
                        "size",
                        "completeness",
                        "manual",
                    )
                }
                if row
                else {}
            )
            items.append(
                dict(
                    item,
                    backup_id=key,
                    retention_layers=layers.get(key, []),
                    lost_restore_point=(
                        {
                            "scope": row["scope"],
                            "group": row["group"],
                            "completed_at": row["completed_at"],
                        }
                        if row
                        else None
                    ),
                    eligible=reason is None,
                    reason=reason,
                    state="not_executed" if reason is None else "blocked",
                    record=row,
                )
            )
        eligible = [item["backup_id"] for item in items if item["eligible"]]
        result = dict(
            id=plan_id or uuid4().hex,
            category="regular",
            state="ready",
            created_at=service.clock.now().isoformat(),
            backup_ids=list(backup_ids),
            repository_id=store.repository.repository_id,
            eligible_ids=eligible,
            items=items,
            estimated_freed_bytes=sum(item["size"] for item in items if item["eligible"]),
            groups_after=[
                dict(
                    scope=scope,
                    group=group,
                    valid_before=len(values),
                    valid_after=sum(row["backup_id"] not in eligible for row in values),
                )
                for (scope, group), values in sorted(groups.items())
            ],
        )
        write(service._path(result["id"]), result)
        return result


def recover(service, value):
    """A deletion intent is not proof of unlink success; never replay it."""
    if value["state"] == "applying":
        for item in value["items"]:
            if item["state"] == "deleting":
                item.update(state="failed", reason="cleanup_result_unknown")
        value.update(state="recovery_blocked", reason="cleanup_result_unknown")
        write(service._path(value["id"]), value)
    return value


def apply(service, value, check=None):
    if value["state"] != "ready":
        return recover(service, value)
    store = BackupStorage(service.home, service.clock)
    if store.repository.repository_id != value["repository_id"]:
        for item in value["items"]:
            if item["eligible"]:
                item.update(state="blocked", reason="source_repository_identity_mismatch")
        value["state"] = "blocked"
        write(service._path(value["id"]), value)
        return value
    value["state"] = "applying"
    write(service._path(value["id"]), value)
    for item in value["items"]:
        if item["backup_id"] not in value["eligible_ids"]:
            continue
        try:
            if check:
                check()
            with store.lock():
                state = store.state()
                row = state["records"].get(item["backup_id"])
                if row != item["record"] or not _verified(store, row):
                    item.update(state="blocked", reason="backup_evidence_mismatch")
                else:
                    with store.repository.deletion(row["path"]) as permitted:
                        minimum, _ = _minimum(store, list(state["records"].values()))
                        if not permitted:
                            item.update(state="blocked", reason="backup_referenced")
                        elif row["backup_id"] in minimum:
                            item.update(state="blocked", reason="minimum_restore_points")
                        elif not _verified(store, row):
                            item.update(state="blocked", reason="backup_evidence_mismatch")
                        else:
                            receipt = dict(
                                backup_id=row["backup_id"],
                                path=row["path"],
                                state="deleting",
                                operation_id=value["id"],
                                at=service.clock.now().isoformat(),
                            )
                            item["state"] = "deleting"
                            write(service._path(value["id"]), value)
                            state["deletions"].append(receipt)
                            write(store.path, state)
                            Path(row["path"]).unlink()
                            receipt["state"] = "deleted"
                            del state["records"][row["backup_id"]]
                            write(store.path, state)
                            item.update(state="deleted", reason=None)
                write(service._path(value["id"]), value)
        except Exception as exc:
            reason = getattr(exc, "code", None) or str(exc)
            if reason == "cancelled":
                value.update(state="stopped", reason="cancelled")
            else:
                failure = (
                    "cleanup_result_unknown" if item["state"] == "deleting" else "cleanup_failed"
                )
                if item["state"] != "deleted":
                    item.update(state="failed", reason=failure)
                value.update(
                    state="partial"
                    if any(i["state"] == "deleted" for i in value["items"])
                    else "blocked",
                    reason=failure,
                )
            write(service._path(value["id"]), value)
            return value
    statuses = {item["state"] for item in value["items"]}
    value["state"] = (
        "completed"
        if statuses == {"deleted"}
        else "partial"
        if "deleted" in statuses
        else "blocked"
    )
    write(service._path(value["id"]), value)
    return value
