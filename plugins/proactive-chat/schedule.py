"""按实例时区计算下一次开口。状态只保存时间、计数和去重标记。"""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time, timedelta

TOPICS = ("good_morning", "long_silence", "festival")
OWNER_REST = {"from": "00:00", "to": "08:00", "active_minutes": 30}
REST = {
    "workday": {"from": "01:00", "to": "09:30"},
    "restday": {"from": "01:30", "to": "10:30"},
}


class Calendar:
    def __init__(self, directory):
        self.directory = directory
        self.years = {}

    def year(self, year):
        if year not in self.years:
            path = self.directory / f"calendar-{year}.json"
            try:
                self.years[year] = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                self.years[year] = {}
        return self.years[year]

    def day(self, day):
        data = self.year(day.year)
        key = day.isoformat()
        complete = "workdays" in data and "holidays" in data
        workday = day.weekday() < 5
        if complete:
            workday = key in data["workdays"] or (workday and key not in data["holidays"])
        return workday, complete, data.get("festivals", {}).get(key, [])

    def future_festivals(self, first_day):
        for path in sorted(self.directory.glob("calendar-[0-9][0-9][0-9][0-9].json")):
            year = int(path.stem.removeprefix("calendar-"))
            if year >= first_day.year:
                for key, names in sorted(self.year(year).get("festivals", {}).items()):
                    if key >= first_day.isoformat() and names:
                        yield date.fromisoformat(key)


@dataclass
class Opening:
    reason: str
    festivals: list = field(default_factory=list)
    fresh_key: str | None = None


@dataclass
class State:
    day: str = ""
    openings: int = 0
    unconfirmed_openings: int = 0
    attempts: int = 0
    declines: int = 0
    unanswered: int = 0
    last_chat: float | None = None
    last_opening: float | None = None
    silence_at: float = 0
    morning_at: float = 0
    morning_done: bool = False
    festival_at: float = 0
    festival_skipped: bool = False
    festivals_done: list = field(default_factory=list)
    retries: dict = field(default_factory=dict)
    submitting: dict | None = None
    pending: dict = field(default_factory=dict)
    used_fresh: dict = field(default_factory=dict)


class Schedule:
    def __init__(self, config, random, tz, directory):
        self.config, self.random, self.tz = config, random, tz
        self.calendar = Calendar(directory)
        self.topics = config.get("topics", TOPICS)
        self.silence = config.get("silence_hours", (2, 3))
        self.retry = config.get("retry_after_decline_hours", (1, 2))
        self.backoff = config.get("backoff_minutes", (30, 60, 120, 240))
        self.max_attempts = config.get("max_attempts_per_day", 40)
        self.max_openings = config.get("max_openings_per_day", 20)
        if (
            self.silence[0] > self.silence[1]
            or self.retry[0] > self.retry[1]
            or list(self.backoff) != sorted(self.backoff)
            or self.max_openings > self.max_attempts
        ):
            raise ValueError("invalid cadence range")
        self.owner = {**OWNER_REST, **config.get("owner_rest", {})}
        self.rest = {
            kind: {**period, **config.get("companion_rest", {}).get(kind, {})}
            for kind, period in REST.items()
        }
        self.state = State()

    def at(self, day, hm):
        wall = datetime.combine(day, time.fromisoformat(hm))
        while True:
            stamp = wall.replace(tzinfo=self.tz, fold=0).timestamp()
            if datetime.fromtimestamp(stamp, self.tz).replace(tzinfo=None) == wall:
                return stamp
            wall += timedelta(minutes=1)

    def date(self, stamp):
        return datetime.fromtimestamp(stamp, self.tz).date()

    def period(self, day):
        workday, _, _ = self.calendar.day(day)
        return self.rest["workday" if workday else "restday"]

    def hours(self, bounds):
        return self.random.uniform(*bounds) * 3600

    def start(self, now, saved=None):
        if saved:
            self.state = State(**saved)
        self.refresh(now)
        stamp = now.timestamp()
        s = self.state
        self.abandon_submission(now)
        if s.silence_at <= stamp:
            s.silence_at = stamp + self.hours(self.silence)
        if self.rest_end(s.morning_at, owner=True) < stamp:
            s.morning_done = True
        if s.festival_at < stamp:
            s.festival_skipped = True

    def refresh(self, now):
        day = now.astimezone(self.tz).date()
        s = self.state
        if s.day >= day.isoformat():
            return False
        s.day = day.isoformat()
        s.openings = s.unconfirmed_openings = s.attempts = s.declines = 0
        s.morning_done = False
        s.festival_skipped = False
        s.festivals_done = []
        jitter = self.config.get("morning_jitter_minutes", 20)
        wake = self.at(day, self.period(day)["to"])
        s.morning_at = wake + 60 * self.random.randint(0, jitter)
        s.festival_at = self.at(day, "10:00") + self.random.randint(0, 600) * 60
        return True

    def dump(self):
        return asdict(self.state)

    def chat(self, now):
        self.refresh(now)
        stamp = now.timestamp()
        s = self.state
        day = self.date(stamp)
        wake = self.at(day, self.period(day)["to"])
        if s.day == day.isoformat() and wake - 3 * 3600 <= stamp <= wake:
            s.morning_done = True
        if s.last_chat is None or stamp > s.last_chat:
            s.last_chat = stamp
            s.silence_at = stamp + self.hours(self.silence)
        if s.last_opening is None or stamp >= s.last_opening:
            s.unanswered = 0

    def rest_end(self, stamp, owner=False):
        # 跨午夜的区间由它开始的那天决定，次日可能恰好换班。
        today = self.date(stamp)
        for day in (today - timedelta(days=1), today):
            period = self.owner if owner else self.period(day)
            start, end = period["from"], period["to"]
            if start == end:
                continue
            until = self.at(day + timedelta(days=end < start), end)
            if self.at(day, start) <= stamp < until:
                return until
        return stamp

    def waits(self, stamp, reason):
        s = self.state
        yield "companion_rest", self.rest_end(stamp)
        if s.last_chat is None or stamp - s.last_chat > self.owner["active_minutes"] * 60:
            yield "owner_rest", self.rest_end(stamp, owner=True)
        if s.last_chat is not None:
            yield "chatting", s.last_chat + self.config.get("chatting_minutes", 10) * 60
        if s.last_opening is not None:
            if s.unanswered:
                wait = self.backoff[min(s.unanswered, len(self.backoff)) - 1]
                yield f"unanswered_{wait}", s.last_opening + wait * 60
            if reason != "fresh":
                yield "minimum_gap", s.last_opening + self.config.get("min_gap_minutes", 30) * 60
        if reason in s.retries:
            retry = s.retries[reason]
            yield retry["cause"], retry["at"]

    def ready_at(self, stamp, reason):
        # 两段作息可能首尾相接。跨过一段以后再次核对另一段。
        for _ in range(6):
            end = max(stamp, *(due for _, due in self.waits(stamp, reason)))
            if end == stamp:
                break
            stamp = end
        return stamp

    def quota(self):
        s = self.state
        return (
            not s.pending
            and s.submitting is None
            and s.attempts + s.unconfirmed_openings < self.max_attempts
            and s.openings + s.unconfirmed_openings < self.max_openings
        )

    def candidates(self, now):
        s = self.state
        day = self.date(now.timestamp())
        names = list(self.calendar.day(day)[2]) if "festival" in self.topics else []
        names = [name for name in names if name not in s.festivals_done]
        wake = self.at(day, self.period(day)["to"])
        chatted_before_wake = s.last_chat is not None and wake - 3 * 3600 <= s.last_chat <= wake
        festival_due = s.festival_at
        silence_due = s.silence_at
        if "good_morning" in self.topics and not s.morning_done and not chatted_before_wake:
            # 主人作息允许明确推迟早安。其余原因不能把一条早安积压到下午。
            until = max(
                self.rest_end(s.morning_at, owner=True),
                wake + self.config.get("morning_jitter_minutes", 20) * 60,
            )
            yield Opening("good_morning", names), s.morning_at, until + 60
            festival_due = max(festival_due, until + 60)
            if now.timestamp() >= wake - self.config.get("min_gap_minutes", 30) * 60:
                silence_due = max(silence_due, s.morning_at)
        if names and not s.festival_skipped:
            yield Opening("festival", names), festival_due, self.at(day, "20:00") + 1
        if "long_silence" in self.topics:
            yield Opening("long_silence"), silence_due, float("inf")

    def next_topic(self, now):
        self.refresh(now)
        stamp = now.timestamp()
        if self.quota():
            for opening, due, end in self.candidates(now):
                if self.ready_at(max(due, stamp), opening.reason) <= stamp < end:
                    return opening
        return None

    def fresh(self, now, key):
        self.refresh(now)
        stamp = now.timestamp()
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        if (
            digest not in self.state.used_fresh
            and self.quota()
            and self.ready_at(stamp, "fresh") <= stamp
        ):
            return Opening("fresh", fresh_key=digest)
        return None

    def reserve(self, opening, now):
        self.state.submitting = {"day": self.state.day, **asdict(opening)}

    def abandon_submission(self, now):
        # 没有编号时无法查询。保留一天额度，跳过原机会，之后的其它话题仍按时安排。
        s = self.state
        opening, s.submitting = s.submitting, None
        if opening is None:
            return
        if opening["day"] == s.day:
            s.unconfirmed_openings += 1
            s.festivals_done.extend(opening["festivals"])
            if opening["reason"] == "good_morning":
                s.morning_done = True
        if opening["reason"] == "long_silence":
            s.silence_at = now.timestamp() + self.hours(self.silence)
        if opening.get("fresh_key"):
            s.used_fresh[opening["fresh_key"]] = True

    def attempt(self, opening, trace, now):
        s = self.state
        s.attempts += 1
        s.pending[trace] = {"day": s.day, "submitted_at": now.timestamp(), **asdict(opening)}
        s.retries.pop(opening.reason, None)
        if opening.reason == "good_morning":
            s.morning_done = True
        elif opening.reason == "long_silence":
            s.silence_at = now.timestamp() + self.hours(self.silence)
        if opening.fresh_key:
            s.used_fresh[opening.fresh_key] = True

    def sent(self, trace, now):
        self.refresh(now)
        s = self.state
        opening = s.pending.pop(trace, None)
        if opening is None:
            return False
        stamp = now.timestamp()
        if self.date(stamp).isoformat() == s.day:
            s.openings += 1
        if s.last_chat is None or stamp > s.last_chat:
            s.unanswered += 1
        s.last_opening = max(stamp, s.last_opening or stamp)
        if opening["day"] == s.day:
            s.festivals_done.extend(opening["festivals"])
        return True

    def retry_at(self, opening, due, cause):
        s = self.state
        reason = opening["reason"]
        # 新鲜事以各自的去重键区分，同一件事已经消耗。其他话题独立重试。
        if reason == "fresh":
            return
        s.retries[reason] = {"at": due, "cause": cause}
        if reason == "good_morning" and opening["day"] == s.day:
            s.morning_done = False
        if reason == "long_silence" and (
            s.last_chat is None or s.last_chat < opening["submitted_at"]
        ):
            s.silence_at = due

    def declined(self, trace, now):
        self.refresh(now)
        s = self.state
        opening = s.pending.pop(trace, None)
        if opening is None:
            return False
        if self.date(now.timestamp()).isoformat() == s.day:
            s.declines += 1
        self.retry_at(opening, now.timestamp() + self.hours(self.retry), "after_decline")
        return True

    def resolve(self, trace, outcome, now):
        if outcome.status == "pending":
            return False
        if outcome.status in {"sent", "unknown"}:
            return self.sent(trace, outcome.sent_at or now)
        if outcome.status == "declined":
            return self.declined(trace, now)
        opening = self.state.pending.pop(trace)
        if outcome.status == "superseded":
            if self.state.last_chat is None or self.state.last_chat < opening["submitted_at"]:
                self.chat(now)
        else:
            self.retry_at(opening, now.timestamp() + 30 * 60, "after_" + outcome.status)
        return True

    def next_time(self, now):
        stamp = now.timestamp()
        times = []
        if self.quota():
            for opening, due, end in self.candidates(now):
                ready = self.ready_at(max(stamp, due), opening.reason)
                if ready < end:
                    times.append(ready)
        if not self.state.pending:
            tomorrow = self.date(stamp) + timedelta(days=1)
            if "long_silence" in self.topics:
                due = max(self.at(tomorrow, "00:00"), self.state.silence_at)
                times.append(self.ready_at(due, "long_silence"))
            if "good_morning" in self.topics:
                wake = self.at(tomorrow, self.period(tomorrow)["to"])
                times.append(self.ready_at(wake, "good_morning"))
            if "festival" in self.topics and not times:
                for holiday in self.calendar.future_festivals(tomorrow):
                    ready = self.ready_at(self.at(holiday, "10:00"), "festival")
                    if ready <= self.at(holiday, "20:00"):
                        times.append(ready)
                        break
        return datetime.fromtimestamp(min(times), self.tz).isoformat() if times else None

    def wait_reason(self, now):
        """给离线时间线说明从上次开口到这次开口间实际生效的约束。"""
        s = self.state
        if s.pending:
            return "awaiting_result"
        if not self.quota():
            return "daily_limit"
        stamp = now.timestamp()
        for code, due in self.waits(stamp, "long_silence"):
            if due > stamp:
                return code
        if s.silence_at > stamp:
            return "silence"
        return "topic_time"
