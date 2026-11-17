import importlib.util
from pathlib import Path

import pytest

from clytie.sdk.testing import FakeIdentity, MockClock
from clytie.sdk.types import Actor


def make():
    path = Path(__file__).parents[1] / "capture.py"
    spec = importlib.util.spec_from_file_location("emoji_capture_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    identity = FakeIdentity(
        "qq-onebot",
        actors={
            "1000000001": Actor(actor_id="owner", role="owner", channel_id="qq-onebot"),
        },
    )
    return module.Capture(identity, MockClock())


def notice(**patch):
    return {
        "post_type": "notice",
        "notice_type": "group_msg_emoji_like",
        "user_id": 1000000001,
        "is_add": True,
        "likes": [{"emoji_id": "1", "count": 1}],
        **patch,
    }


@pytest.mark.parametrize("actor", [1000000001, "1000000001"])
def test_owner_capture_only_returns_identifiers(actor):
    c = make()
    c.start()
    c.feed(notice(user_id=actor))
    assert c.status()["ids"] == ["1"]
    assert "1000000001" not in str(c.status())


@pytest.mark.parametrize(
    "patch",
    [
        {"user_id": None},
        {"user_id": True},
        {"user_id": 1.5},
        {"user_id": " 1000000001"},
        {"user_id": "bad"},
        {"user_id": 1000000002},
        {"is_add": False},
        {"is_add": None},
        {"is_add": 1},
        {"likes": {}},
        {"likes": []},
        {"likes": [{"emoji_id": None}]},
        {"likes": [{"emoji_id": True}]},
    ],
)
def test_rejected_notice_counts_once_without_private_fields(patch):
    c = make()
    c.start()
    c.feed(notice(**patch))
    assert c.status()["ids"] == []
    assert c.status()["rejected"] == 1
    assert "user_id" not in str(c.status())


def test_missing_actor_and_is_add_rejected():
    for field in ("user_id", "is_add"):
        c = make()
        c.start()
        event = notice()
        del event[field]
        c.feed(event)
        assert c.status()["rejected"] == 1


def test_four_in_order_timeout_and_no_notice_support():
    c = make()
    c.start()
    for n in range(4):
        c.feed(notice(likes=[{"emoji_id": str(n + 1)}]))
    assert c.status()["state"] == "complete"
    assert c.status()["ids"] == ["1", "2", "3", "4"]
    c = make()
    c.start()
    c.clock.advance(180)
    assert c.status()["state"] == "unsupported"
    c = make()
    c.start()
    c.feed(notice())
    c.clock.advance(180)
    assert c.status()["state"] == "incomplete"
    c.feed(notice(likes=[{"emoji_id": "2"}]))
    assert c.status()["ids"] == ["1"]
