import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from random import Random
from zoneinfo import ZoneInfo

import pytest

ROOT = Path(__file__).resolve().parents[1]
TZ = timezone(timedelta(hours=8))


def local(day, hm):
    return datetime.fromisoformat(f"{day}T{hm}").replace(tzinfo=TZ)


@pytest.fixture
def module():
    path = ROOT / "schedule.py"
    assert path.is_file(), "缺少主动私聊插件的调度器"
    spec = importlib.util.spec_from_file_location("proactive_schedule_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def calendar_dir(tmp_path):
    calendar = {
        "holidays": ["2026-01-01"],
        "workdays": ["2026-01-04"],
        "festivals": {"2026-01-01": ["测试节"]},
    }
    (tmp_path / "calendar-2026.json").write_text(json.dumps(calendar), encoding="utf-8")
    (tmp_path / "calendar-2027.json").write_text(
        json.dumps({"festivals": {"2027-01-01": ["测试节"]}}), encoding="utf-8"
    )
    return tmp_path


def plan(module, calendar_dir, now, **config):
    p = module.Schedule({"conversation": "test_private", **config}, Random(27), TZ, calendar_dir)
    p.start(now)
    return p


@pytest.mark.parametrize(
    "day,workday,complete,names",
    [
        ("2026-01-01", False, True, ["测试节"]),
        ("2026-01-02", True, True, []),
        ("2026-01-03", False, True, []),
        ("2026-01-04", True, True, []),
        ("2027-01-01", True, False, ["测试节"]),
        ("2028-01-01", False, False, []),
    ],
)
def test_rest_calendar(module, calendar_dir, day, workday, complete, names):
    date = local(day, "00:00").date()
    calendar = module.Calendar(calendar_dir)
    assert calendar.day(date) == (workday, complete, names)


@pytest.mark.parametrize(
    "config",
    [
        {"silence_hours": [3, 2]},
        {"retry_after_decline_hours": [2, 1]},
        {"backoff_minutes": [60, 30]},
        {"max_openings_per_day": 50, "max_attempts_per_day": 40},
    ],
)
def test_invalid_cross_field_configuration(module, calendar_dir, config):
    with pytest.raises(ValueError):
        plan(module, calendar_dir, local("2026-01-02", "00:00"), **config)


def test_morning_combines_festivals_and_waits_until_owner_is_awake(module, calendar_dir):
    day = "2026-01-01"
    p = plan(
        module,
        calendar_dir,
        local(day, "00:00"),
        topics=["good_morning", "festival"],
        morning_jitter_minutes=0,
        owner_rest={"from": "00:00", "to": "11:00", "active_minutes": 30},
    )
    assert p.next_topic(local(day, "10:30")) is None
    now = local(day, "11:00")
    opening = p.next_topic(now)
    assert opening.reason == "good_morning" and opening.festivals == ["测试节"]
    p.attempt(opening, "a" * 32, now)
    p.sent("a" * 32, now)
    assert p.next_topic(local(day, "19:00")) is None


def test_recent_chat_suppresses_morning_festival_can_still_run(module, calendar_dir):
    day = "2026-01-01"
    p = plan(
        module,
        calendar_dir,
        local(day, "00:00"),
        topics=["good_morning", "festival"],
        morning_jitter_minutes=0,
    )
    p.chat(local(day, "09:00"))
    assert p.next_topic(local(day, "10:30")) is None or (
        p.next_topic(local(day, "10:30")).reason == "festival"
    )
    after = p.next_topic(local(day, "19:59"))
    assert after.reason == "festival" and after.festivals == ["测试节"]
    p.attempt(after, "b" * 32, local(day, "19:59"))
    p.sent("b" * 32, local(day, "19:59"))
    assert p.next_topic(local(day, "20:00")) is None


def test_long_silence_uses_a_single_random_deadline_per_chat(module, calendar_dir):
    day = "2026-01-02"
    now = local(day, "10:00")
    p = plan(module, calendar_dir, now, topics=["long_silence"])
    assert p.next_topic(now + timedelta(minutes=119)) is None
    p.chat(now + timedelta(hours=1))
    assert p.next_topic(now + timedelta(hours=2, minutes=59)) is None
    assert p.next_topic(now + timedelta(hours=4)).reason == "long_silence"
    deadline = p.state.silence_at
    assert p.next_topic(now + timedelta(hours=4)).reason == "long_silence"
    assert p.state.silence_at == deadline


def test_sent_counts_once_and_unanswered_delays_apply_to_fresh(module, calendar_dir):
    now = local("2026-01-02", "10:00")
    p = plan(module, calendar_dir, now, topics=[])
    for index, gap in enumerate([30, 60, 120, 240, 240], start=1):
        key = f"fake-story-{index}"
        fresh = p.fresh(now, key)
        assert fresh and fresh.reason == "fresh"
        trace = f"{index:032x}"
        p.attempt(fresh, trace, now)
        assert p.state.openings == index - 1
        p.sent(trace, now)
        p.sent(trace, now)
        assert p.state.openings == index
        assert p.state.unanswered == index
        assert p.fresh(now + timedelta(minutes=gap - 1), "different") is None
        now += timedelta(minutes=gap)
        assert p.fresh(now, key) is None
    p.chat(now)
    assert p.state.unanswered == 0
    assert p.fresh(now + timedelta(minutes=9), "different") is None
    assert p.fresh(now + timedelta(minutes=10), "different") is not None


def test_fresh_skips_only_the_minimum_gap(module, calendar_dir):
    now = local("2026-01-02", "10:00")
    p = plan(module, calendar_dir, now, topics=["long_silence"], min_gap_minutes=240)
    p.attempt(p.fresh(now, "first"), "1" * 32, now)
    p.sent("1" * 32, now)
    p.chat(now + timedelta(minutes=1))
    later = now + timedelta(hours=3, minutes=5)
    assert p.next_topic(later) is None
    assert p.fresh(later, "second")
    assert p.next_topic(now + timedelta(hours=4)).reason == "long_silence"


def test_decline_relaxes_only_its_own_topic_after_a_random_hour(module, calendar_dir):
    start = local("2026-01-02", "10:00")
    p = plan(module, calendar_dir, start, topics=["long_silence"])
    now = start + timedelta(hours=3)
    p.attempt(p.next_topic(now), "e" * 32, now)
    p.declined("e" * 32, now)
    assert p.state.declines == 1 and p.state.openings == p.state.unanswered == 0
    assert p.next_topic(now + timedelta(minutes=59)) is None
    assert p.fresh(now, "other-topic") is not None
    assert p.next_topic(now + timedelta(hours=2)).reason == "long_silence"


def test_daily_limits_use_local_midnight_and_separate_attempts(module, calendar_dir):
    start = local("2026-01-02", "10:00")
    p = plan(
        module,
        calendar_dir,
        start,
        topics=[],
        max_openings_per_day=1,
        max_attempts_per_day=2,
        retry_after_decline_hours=[0.5, 0.5],
    )
    for index in range(2):
        now = start + timedelta(hours=index)
        trace = f"{index:032x}"
        p.attempt(p.fresh(now, f"story-{index}"), trace, now)
        p.declined(trace, now)
    assert p.state.attempts == 2 and p.state.openings == 0
    assert p.fresh(start + timedelta(hours=2), "third") is None
    tomorrow = start + timedelta(days=1, hours=1)
    fresh = p.fresh(tomorrow, "third")
    assert p.state.attempts == p.state.declines == 0 and fresh
    p.attempt(fresh, "a" * 32, tomorrow)
    p.sent("a" * 32, tomorrow)
    assert p.fresh(tomorrow + timedelta(hours=1), "fourth") is None


def test_rest_and_owner_active_exception(module, calendar_dir):
    day = "2026-01-02"
    p = plan(module, calendar_dir, local(day, "00:00"), topics=[])
    assert p.fresh(local(day, "00:15"), "first") is None
    p.chat(local(day, "00:20"))
    assert p.fresh(local(day, "00:29"), "first") is None
    assert p.fresh(local(day, "00:30"), "first")
    assert p.fresh(local(day, "00:51"), "first") is None
    p.chat(local(day, "01:01"))
    assert p.fresh(local(day, "01:15"), "first") is None


def test_restart_restores_counters_and_skips_missed_times(module, calendar_dir):
    start = local("2026-01-02", "00:00")
    p = plan(module, calendar_dir, start, morning_jitter_minutes=0)
    morning = local("2026-01-02", "09:30")
    p.attempt(p.next_topic(morning), "a" * 32, morning)
    p.sent("a" * 32, morning)
    restart = local("2026-01-03", "15:00")
    q = module.Schedule(p.config, Random(27), TZ, calendar_dir)
    q.start(restart, p.dump())
    assert q.state.unanswered == 1
    assert q.state.openings == q.state.attempts == 0
    assert q.next_topic(restart) is None
    assert q.next_topic(restart + timedelta(hours=3)).reason == "long_silence"


def test_festival_waits_for_planned_morning_even_if_its_backup_time_is_earlier(
    module, calendar_dir
):
    day = "2026-01-01"
    p = plan(
        module,
        calendar_dir,
        local(day, "00:00"),
        topics=["good_morning", "festival"],
        morning_jitter_minutes=20,
    )
    p.state.morning_at = local(day, "10:50").timestamp()
    p.state.festival_at = local(day, "10:00").timestamp()
    assert p.next_topic(local(day, "10:35")) is None
    assert p.next_topic(local(day, "10:50")).festivals == ["测试节"]


def test_pre_wake_chat_still_cancels_morning_after_another_chat(module, calendar_dir):
    day = "2026-01-02"
    p = plan(
        module,
        calendar_dir,
        local(day, "00:00"),
        topics=["good_morning"],
        morning_jitter_minutes=20,
    )
    p.state.morning_at = local(day, "09:50").timestamp()
    p.chat(local(day, "08:10"))
    p.chat(local(day, "09:32"))
    assert p.next_topic(local(day, "09:50")) is None


def test_pending_delivery_reserves_a_place_across_local_midnight(module, calendar_dir):
    day = "2026-01-02"
    now = local(day, "23:50")
    p = plan(module, calendar_dir, now, topics=[], max_openings_per_day=1)
    p.attempt(p.fresh(now, "pending"), "1" * 32, now)
    tomorrow = local("2026-01-03", "11:00")
    assert p.fresh(tomorrow, "later") is None
    p.sent("1" * 32, tomorrow)
    assert p.state.openings == 1 and p.state.attempts == 0


def test_rate_limit_status_gives_next_possible_date(module, calendar_dir):
    now = local("2026-01-02", "12:00")
    p = plan(module, calendar_dir, now, topics=["long_silence"], max_openings_per_day=1)
    p.attempt(p.fresh(now, "sent"), "a" * 32, now)
    p.sent("a" * 32, now)
    assert datetime.fromisoformat(p.next_time(now)) == local("2026-01-03", "10:30")


def test_wait_reason_records_the_constraint_active_at_each_tick(module, calendar_dir):
    now = local("2026-01-02", "00:00")
    p = plan(module, calendar_dir, now, topics=["long_silence"])
    assert p.wait_reason(now) == "owner_rest"
    assert p.wait_reason(local("2026-01-02", "02:00")) == "companion_rest"
    awake = local("2026-01-02", "12:00")
    p.attempt(p.next_topic(awake), "b" * 32, awake)
    p.sent("b" * 32, awake)
    assert p.wait_reason(awake + timedelta(minutes=1)) == "unanswered_30"
    p.chat(awake + timedelta(minutes=2))
    assert p.wait_reason(awake + timedelta(minutes=3)) == "chatting"
    assert p.wait_reason(awake + timedelta(minutes=35)) == "silence"


def test_overnight_silence_does_not_take_the_morning_slot(module, calendar_dir):
    day = "2026-01-02"
    p = plan(module, calendar_dir, local(day, "00:00"))
    p.state.morning_at = local(day, "09:47").timestamp()
    assert p.next_topic(local(day, "09:31")) is None
    assert p.next_topic(local(day, "09:47")).reason == "good_morning"


def test_restarting_during_owner_rest_preserves_the_future_morning(module, calendar_dir):
    day = "2026-01-01"
    p = plan(
        module,
        calendar_dir,
        local(day, "00:00"),
        morning_jitter_minutes=0,
        topics=["good_morning", "festival"],
        owner_rest={"from": "00:00", "to": "12:00", "active_minutes": 30},
    )
    p.state.festival_at = local(day, "10:00").timestamp()
    q = module.Schedule(p.config, Random(27), TZ, calendar_dir)
    q.start(local(day, "11:00"), p.dump())
    assert q.next_topic(local(day, "11:59")) is None
    opening = q.next_topic(local(day, "12:00"))
    assert opening and opening.reason == "good_morning" and opening.festivals == ["测试节"]


def test_nonexistent_wake_time_uses_the_first_valid_local_minute(module, calendar_dir):
    zone = ZoneInfo("America/New_York")
    p = module.Schedule(
        {
            "conversation": "test_private",
            "topics": ["good_morning"],
            "morning_jitter_minutes": 0,
            "owner_rest": {"from": "00:00", "to": "00:00"},
            "companion_rest": {"restday": {"from": "01:00", "to": "02:30"}},
        },
        Random(27),
        zone,
        calendar_dir,
    )
    p.start(datetime(2026, 3, 8, 0, 0, tzinfo=zone))
    now = datetime(2026, 3, 8, 3, 0, tzinfo=zone)
    assert p.next_topic(now) and p.next_topic(now).reason == "good_morning"


def test_restart_without_trace_consumes_the_original_opportunity_only(module, calendar_dir):
    day = "2026-01-01"
    p = plan(
        module, calendar_dir, local(day, "00:00"), morning_jitter_minutes=0, max_openings_per_day=2
    )
    now = local(day, "10:30")
    morning = p.next_topic(now)
    assert morning.festivals == ["测试节"]
    # 模拟在提交与保存返回编号之间退出。
    p.reserve(morning, now)
    q = module.Schedule(p.config, Random(27), TZ, calendar_dir)
    q.start(local(day, "10:31"), p.dump())
    assert q.state.openings == q.state.unanswered == q.state.declines == 0
    assert q.state.attempts == 0
    assert q.next_topic(local(day, "10:32")) is None
    later = local(day, "14:00")
    opening = q.next_topic(later)
    assert opening and opening.reason == "long_silence" and not opening.festivals
    q.attempt(opening, "a" * 32, later)
    q.sent("a" * 32, later)
    assert q.next_topic(local(day, "19:00")) is None
    assert q.next_topic(local("2026-01-02", "11:00")) is not None


def test_default_daily_caps_on_distinct_fresh_stories(module, calendar_dir):
    now = local("2026-01-02", "12:00")
    p = plan(module, calendar_dir, now, topics=[])
    for index in range(40):
        story = p.fresh(now, f"fictional-declined-{index}")
        assert story
        trace = f"{index:032x}"
        p.attempt(story, trace, now)
        p.declined(trace, now)
    assert p.state.declines == p.state.attempts == 40
    assert p.fresh(now + timedelta(hours=3), "fictional-forty-first") is None

    p = plan(module, calendar_dir, now, topics=[])
    for index in range(20):
        story = p.fresh(now, f"fictional-sent-{index}")
        assert story
        trace = f"{index:032x}"
        p.attempt(story, trace, now)
        p.sent(trace, now)
        now += timedelta(minutes=1)
        p.chat(now)
        now += timedelta(minutes=10)
    assert p.state.openings == p.state.attempts == 20
    assert p.fresh(now, "fictional-twenty-first") is None


def test_restart_without_trace_reserves_the_last_possible_attempt(module, calendar_dir):
    now = local("2026-01-02", "12:00")
    p = plan(module, calendar_dir, now, topics=[], max_openings_per_day=2, max_attempts_per_day=2)
    p.attempt(p.fresh(now, "fictional-decline"), "a" * 32, now)
    p.declined("a" * 32, now)
    p.reserve(p.fresh(now, "fictional-uncertain"), now)
    restarted = module.Schedule(p.config, Random(27), TZ, calendar_dir)
    restarted.start(now, p.dump())
    assert restarted.state.attempts == 1
    assert restarted.state.openings == restarted.state.unanswered == 0
    assert restarted.fresh(now, "fictional-third") is None
    assert restarted.fresh(now + timedelta(days=1), "fictional-next-day")


def test_old_unnumbered_submission_does_not_use_a_later_days_quota(module, calendar_dir):
    now = local("2026-01-02", "12:00")
    p = plan(module, calendar_dir, now, topics=[], max_openings_per_day=1, max_attempts_per_day=1)
    p.reserve(p.fresh(now, "fictional-original"), now)
    restarted = module.Schedule(p.config, Random(27), TZ, calendar_dir)
    later = now + timedelta(days=3)
    restarted.start(later, p.dump())
    assert restarted.state.openings == restarted.state.attempts == 0
    assert restarted.fresh(later, "fictional-original") is None
    assert restarted.fresh(later, "fictional-new-opportunity")


def test_status_with_only_festivals_gives_next_known_date(module, calendar_dir):
    now = local("2026-12-22", "12:00")
    p = plan(module, calendar_dir, now, topics=["festival"])
    assert datetime.fromisoformat(p.next_time(now)) == local("2027-01-01", "10:00")
