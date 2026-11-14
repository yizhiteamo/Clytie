"""Pure retention selection; execution must revalidate ownership and references."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class BackupRecord:
    backup_id: str
    path: str
    scope: str
    group: str
    completed_at: datetime
    size: int
    completeness: str = "complete"
    manual: bool = False
    referenced: bool = False
    verified: bool = True
    owned: bool = True


@dataclass(frozen=True)
class RetentionConfig:
    recent_memory: int = 12
    daily_days: int = 30
    weekly_weeks: int = 52
    monthly_months: int | None = None
    minimum_per_group: int = 3
    target_bytes: int | None = 20 * 1024**3

    def __post_init__(self):
        if (
            self.minimum_per_group < 3
            or self.recent_memory < self.minimum_per_group
            or self.daily_days < 0
            or self.weekly_weeks < 0
            or (self.monthly_months is not None and self.monthly_months < 0)
            or (self.target_bytes is not None and self.target_bytes <= 0)
        ):
            raise ValueError("invalid_retention_config")


@dataclass(frozen=True)
class RetentionPlan:
    delete: tuple[str, ...]
    capacity_lost: tuple[str, ...]
    protected: tuple[str, ...]
    old_bytes_remaining: int
    status: str


def calendar_bucket(completed_at, now, config):
    """Shared retention calendar classification for selection and plan display."""
    local = completed_at.astimezone(now.tzinfo)
    age_days = (now.date() - local.date()).days
    age_months = (now.year - local.year) * 12 + now.month - local.month
    if age_days < config.daily_days:
        return ("day", local.date())
    if age_days < config.weekly_weeks * 7:
        return ("week", *local.isocalendar()[:2])
    if config.monthly_months is None or age_months < config.monthly_months:
        return ("month", local.year, local.month)
    return None


def plan_retention(
    records: list[BackupRecord],
    config: RetentionConfig,
    *,
    now: datetime,
    started_bytes: int,
    new_backup_id: str,
) -> RetentionPlan:
    """Select owned verified automatic files after a successful new publication.

    `now` must carry the instance timezone. `started_bytes` is S recorded under
    the serialized backup lock, not a total recomputed after publication.
    Unknown/unverified files are excluded from deletion and must be accounted
    separately by the caller; this function never guesses ownership by name.
    """
    if now.tzinfo is None or started_bytes < 0:
        raise ValueError("invalid_retention_input")
    if len({r.backup_id for r in records}) != len(records) or len({r.path for r in records}) != len(
        records
    ):
        raise ValueError("duplicate_backup_record")
    for r in records:
        if (
            r.completed_at.tzinfo is None
            or r.size < 0
            or r.scope not in {"memory", "instance"}
            or r.completeness not in {"complete", "partial"}
        ):
            raise ValueError("invalid_backup_record")
    ordered = sorted(records, key=lambda r: (r.completed_at, r.backup_id))
    automatic = [r for r in ordered if not r.manual]
    old_total = sum(r.size for r in automatic if r.backup_id != new_backup_id)
    new = next(
        (
            r
            for r in records
            if r.backup_id == new_backup_id and r.owned and r.verified and not r.manual
        ),
        None,
    )
    if new is None:
        return RetentionPlan((), (), (), old_total, "skipped")
    protected = {
        r.backup_id for r in ordered if r.manual or r.referenced or not r.owned or not r.verified
    }
    protected.add(new_backup_id)
    groups = defaultdict(list)
    partials = []
    for r in automatic:
        if r.owned and r.verified:
            if r.completeness == "partial":
                partials.append(r)
            else:
                groups[(r.scope, r.group)].append(r)
    expected = set(protected)
    expected.update(r.backup_id for r in partials[-3:])
    for (scope, _), group in groups.items():
        protected.update(r.backup_id for r in group[-config.minimum_per_group :])
        if scope == "memory":
            expected.update(r.backup_id for r in group[-config.recent_memory :])
        buckets = {}
        for r in group:
            bucket = calendar_bucket(r.completed_at, now, config)
            if bucket is None:
                continue
            buckets[bucket] = r.backup_id
        expected.update(buckets.values())
    expected.update(protected)
    deleted = set()
    for r in automatic:
        if r.backup_id not in expected and (
            new.completeness == "complete" or r.completeness == "partial"
        ):
            deleted.add(r.backup_id)
    remaining = old_total - sum(r.size for r in automatic if r.backup_id in deleted)
    lost = []
    if (
        new.completeness == "complete"
        and config.target_bytes is not None
        and started_bytes > config.target_bytes
    ):
        for r in automatic:
            if remaining <= config.target_bytes:
                break
            if r.backup_id not in protected and r.backup_id not in deleted:
                deleted.add(r.backup_id)
                remaining -= r.size
                if r.backup_id in expected:
                    lost.append(r.path)
    status = "ok"
    if config.target_bytes is not None:
        protected_size = sum(r.size for r in automatic if r.backup_id in protected)
        if protected_size > config.target_bytes or (
            started_bytes > config.target_bytes and remaining > config.target_bytes
        ):
            status = "retention_protected"
    return RetentionPlan(
        tuple(r.path for r in ordered if r.backup_id in deleted),
        tuple(lost),
        tuple(r.path for r in ordered if r.backup_id in protected),
        remaining,
        status,
    )


def plan_mirror_retention(
    records: list[BackupRecord],
    *,
    now: datetime,
    recent: int = 7,
    monthly_months: int = 12,
    minimum: int = 3,
) -> RetentionPlan:
    """Select only receipted mirror files; historical repositories must not call this."""
    if minimum < 3 or recent < minimum or monthly_months < 0 or now.tzinfo is None:
        raise ValueError("invalid_retention_config")
    ordered = sorted(records, key=lambda r: (r.completed_at, r.backup_id))
    if len({r.backup_id for r in ordered}) != len(ordered) or len({r.path for r in ordered}) != len(
        ordered
    ):
        raise ValueError("duplicate_backup_record")
    protected = {
        r.backup_id
        for r in ordered
        if r.referenced or r.manual or not r.owned or not r.verified or r.completeness != "complete"
    }
    expected = set(protected)
    groups = defaultdict(list)
    for r in ordered:
        if r.owned and r.verified and r.completeness == "complete" and not r.manual:
            groups[(r.scope, r.group)].append(r)
    for group in groups.values():
        protected.update(r.backup_id for r in group[-minimum:])
        expected.update(r.backup_id for r in group[-recent:])
        months = {}
        for r in group:
            local = r.completed_at.astimezone(now.tzinfo)
            age = (now.year - local.year) * 12 + now.month - local.month
            if age < monthly_months:
                months[(local.year, local.month)] = r.backup_id
        expected.update(months.values())
    expected.update(protected)
    deleted = tuple(r.path for r in ordered if r.backup_id not in expected)
    return RetentionPlan(
        deleted,
        (),
        tuple(r.path for r in ordered if r.backup_id in protected),
        sum(r.size for r in ordered if r.path not in deleted),
        "ok",
    )
