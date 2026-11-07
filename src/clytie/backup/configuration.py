"""实例 backup 配置计划与精确 YAML 写回；不执行备份或删除备份包。"""

from __future__ import annotations

import hashlib
import io
import json
import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from ruamel.yaml import YAML
from ruamel.yaml.nodes import MappingNode, ScalarNode
from ruamel.yaml.tokens import AliasToken, AnchorToken

from clytie.api.config_write import ConfigWriter
from clytie.config import load_home, thaw
from clytie.plugin_host.locking import file_lock
from clytie.plugin_host.snapshots import atomic_read, atomic_write, canonical, contained


def _hash(data):
    return hashlib.sha256(data).hexdigest()


@contextmanager
def config_commit_lock(home):
    """捕获配置与最后原子提交共享；不得在此门闩中持有业务数据库锁。"""
    home = Path(home)
    path = contained(home, home / "supervisor/config-commit.lock")
    with file_lock(path, {"operation": "config_commit"}):
        yield


def patch_backup_source(source, patch):
    if not isinstance(patch, dict):
        raise ValueError("config_invalid")
    yaml = YAML(typ="rt")
    try:
        if any(isinstance(token, (AliasToken, AnchorToken)) for token in yaml.scan(source)):
            raise ValueError("config_not_writable")
        tree = yaml.compose(source)
    except ValueError:
        raise
    except Exception:
        raise ValueError("config_not_writable") from None
    newline = "\r\n" if "\r\n" in source else "\n"
    edits = []

    def children(node):
        if not isinstance(node, MappingNode) or node.flow_style:
            raise ValueError("config_not_writable")
        values = {}
        for key, value in node.value:
            if not isinstance(key, ScalarNode) or key.value in values or key.value == "<<":
                raise ValueError("config_not_writable")
            values[key.value] = value
        return values

    def block(value, indent):
        stream = io.StringIO()
        encoder = YAML(typ="safe")
        encoder.default_flow_style = False
        encoder.dump(value, stream)
        return "".join(" " * indent + line + newline for line in stream.getvalue().splitlines())

    def merge(node, change):
        current = children(node)
        missing = {}
        for key, value in change.items():
            if key not in current:
                missing[key] = value
            elif isinstance(value, dict):
                merge(current[key], value)
            else:
                child = current[key]
                edits.append(
                    (
                        child.start_mark.index,
                        child.end_mark.index,
                        json.dumps(value, ensure_ascii=False, allow_nan=False),
                    )
                )
        if missing:
            if not node.value:
                raise ValueError("config_not_writable")
            mark = node.value[0][0].start_mark
            start = source.rfind("\n", 0, mark.index) + 1
            edits.append((start, start, block(missing, mark.column)))

    root = children(tree)
    if "backup" in root:
        merge(root["backup"], patch)
    else:
        prefix = "" if source.endswith("\n") else newline
        edits.append((len(source), len(source), prefix + block({"backup": patch}, 0)))
    for start, end, value in sorted(edits, reverse=True):
        source = source[:start] + value + source[end:]
    return source


def _confirmation(before, after):
    reasons = []
    if before["enabled"] and not after["enabled"]:
        reasons.append("backup_disabled")
    if before["memory"]["enabled"] and not after["memory"]["enabled"]:
        reasons.append("memory_backup_disabled")
    if before["mirror"]["path"] != after["mirror"]["path"]:
        reasons.append("mirror_path_changed")
    for section, fields in [
        (
            "retention",
            ["recent_memory", "daily_days", "weekly_weeks", "monthly_months", "minimum_per_group"],
        ),
        ("mirror", ["recent", "monthly_months", "minimum"]),
    ]:
        for name in fields:
            old, new = before[section][name], after[section][name]
            if new is not None and (old is None or new < old):
                reasons.append(f"{section}.{name}_reduced")
    return reasons


def _next_schedule(now, zone, schedule):
    tz = ZoneInfo(zone)
    date = now.astimezone(tz).date()
    hour, minute = map(int, schedule["at"].split(":"))
    for offset in range(8):
        day = date + timedelta(days=offset)
        if schedule["days"] and day.isoweekday() not in schedule["days"]:
            continue
        wall = datetime(day.year, day.month, day.day, hour, minute)
        while wall.date() == day:
            candidates = [
                wall.replace(tzinfo=tz, fold=fold).astimezone(timezone.utc) for fold in (0, 1)
            ]
            candidates = [
                instant
                for instant in candidates
                if instant.astimezone(tz).replace(tzinfo=None) == wall
            ]
            if candidates:
                instant = min(candidates)
                if instant >= now:
                    return instant.isoformat()
                break
            wall += timedelta(minutes=1)
    return None


class BackupConfigWriter(ConfigWriter):
    def __init__(self, home, clock, *, repo_root=None):
        super().__init__(home, clock)
        self.home = self.home.resolve()
        self.repo_root = repo_root
        self.path = contained(self.home, self.home / "clytie.local.yaml")
        self.validate = lambda: load_home(self.home, repo_root=self.repo_root).valid

    def _report(self, source):
        report = load_home(self.home, repo_root=self.repo_root, _instance_source=source)
        if not report.valid:
            raise ValueError("config_invalid")
        return report

    def _operation(self):
        return file_lock(
            contained(self.home, self.home / "supervisor/backup-config.lock"),
            {"operation": "backup_config"},
        )

    def _plan_path(self, plan_id):
        if not re.fullmatch("[a-f0-9]{64}", plan_id):
            raise ValueError("invalid_plan_id")
        return contained(
            self.home, self.home / "supervisor/backup-config-plans" / (plan_id + ".json")
        )

    def _mirror_manager(self):
        from clytie.config import repository_root

        from .mirror import MirrorManager
        from .storage import BackupStorage

        storage = BackupStorage(self.home, self.clock)
        return MirrorManager(
            self.home / "supervisor/backup-mirror",
            storage.root,
            self.clock,
            owner_id=storage.instance_id(),
            forbidden_roots=(self.home, repository_root()),
        )

    def plan(self, patch):
        with self._operation():
            before = atomic_read(self.path)
            old = self._report(before)
            after = patch_backup_source(before.decode("utf-8"), patch).encode("utf-8")
            new = self._report(after)
            before_cfg, after_cfg = thaw(old.instance["backup"]), thaw(new.instance["backup"])
            reasons = _confirmation(before_cfg, after_cfg)
            payload = {"source_hash": _hash(before), "candidate_hash": _hash(after), "patch": patch}
            mirror_plan = None
            if after_cfg["mirror"]["enabled"]:
                from .storage import BackupStorage

                manager = self._mirror_manager()
                state = manager.state()
                current = state["candidate"] or state["active"]
                target = Path(after_cfg["mirror"]["path"]).expanduser()
                if not target.is_absolute():
                    target = self.home / target
                if current is None or Path(current["path"]).resolve() != target.resolve():
                    storage = BackupStorage(self.home, self.clock)
                    mirror_plan = manager.plan(
                        target, [storage.record(row) for row in storage.list()]
                    )
                    payload["mirror_plan_id"] = mirror_plan["plan_id"]
                    reasons.append("mirror_repository_acceptance")
                else:
                    manager._validate(current)
            plan_id = _hash(canonical(payload))
            record = {
                **payload,
                "plan_id": plan_id,
                "requires_confirmation": bool(reasons),
                "confirmation_reasons": reasons,
                "applied": False,
                "impact": {
                    "schedule": after_cfg["schedule"],
                    "next_due": _next_schedule(
                        self.clock.now(), new.instance["timezone"], after_cfg["schedule"]
                    )
                    if after_cfg["enabled"]
                    else None,
                    "retention": after_cfg["retention"],
                    "mirror": after_cfg["mirror"],
                    "mirror_repository": mirror_plan,
                    "cleanup": "next_successful_automatic_backup",
                    "target_is_hard_limit": False,
                },
            }
            path = self._plan_path(plan_id)
            if not path.exists():
                atomic_write(path, canonical(record))
            return record

    def apply(self, plan_id, *, confirmed=False):
        with self._operation():
            path = self._plan_path(plan_id)
            record = json.loads(atomic_read(path))
            payload = {key: record[key] for key in ("source_hash", "candidate_hash", "patch")}
            if "mirror_plan_id" in record:
                payload["mirror_plan_id"] = record["mirror_plan_id"]
            if _hash(canonical(payload)) != plan_id:
                raise ValueError("plan_stale")
            before = atomic_read(self.path)
            if record["applied"] and _hash(before) == record["candidate_hash"]:
                return {"saved": True, "plan_id": plan_id}
            if _hash(before) != record["source_hash"]:
                raise ValueError("plan_stale")
            old = self._report(before)
            after = patch_backup_source(before.decode("utf-8"), record["patch"]).encode("utf-8")
            new = self._report(after)
            if _hash(after) != record["candidate_hash"]:
                raise ValueError("plan_stale")
            if (
                _confirmation(thaw(old.instance["backup"]), thaw(new.instance["backup"]))
                or record.get("mirror_plan_id")
            ) and not confirmed:
                raise ValueError("confirmation_required")
            if record.get("mirror_plan_id"):
                self._mirror_manager().confirm(record["mirror_plan_id"])
            history = contained(self.home, self.home / "config-history")
            suffix = self.clock.now().strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid4().hex
            backup = history / ("clytie.local.yaml." + suffix)
            atomic_write(backup, before)
            with config_commit_lock(self.home):
                if _hash(atomic_read(self.path)) != record["source_hash"]:
                    raise ValueError("plan_stale")
                atomic_write(self.path, after)
            try:
                valid = self.validate()
            except Exception:
                valid = False
            if not valid:
                with config_commit_lock(self.home):
                    if _hash(atomic_read(self.path)) != record["candidate_hash"]:
                        raise ValueError("config_changed_during_validation")
                    atomic_write(self.path, before)
                raise ValueError("config_invalid_after_write")
            record["applied"] = True
            atomic_write(path, canonical(record))
            owned = sorted(
                p
                for p in history.iterdir()
                if re.fullmatch(r"clytie\.local\.yaml\.[0-9TZ]+-[a-f0-9]+", p.name)
            )
            for old_path in owned[:-10]:
                contained(self.home, old_path)
                if not old_path.is_file():
                    raise ValueError("config_not_writable")
                old_path.unlink()
            return {"saved": True, "plan_id": plan_id}
