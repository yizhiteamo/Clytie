"""Bounded, memory-only collection of owner reaction identifiers."""

import re
from uuid import uuid4


def identifier(value):
    return (type(value) is int and value >= 0) or (
        isinstance(value, str) and re.fullmatch(r"[0-9]{1,32}", value) is not None
    )


class Capture:
    def __init__(self, identity, clock):
        self.identity, self.clock = identity, clock
        self.session = None
        self.ids = []
        self.rejected = self.seen = 0
        self.deadline = 0
        self.state = "idle"

    def start(self, seconds=180):
        if type(seconds) not in (int, float) or not 0 < seconds <= 180:
            raise ValueError("invalid_duration")
        if self.status()["state"] == "collecting":
            raise ValueError("capture_busy")
        self.session = uuid4().hex
        self.ids = []
        self.rejected = self.seen = 0
        self.deadline = self.clock.monotonic() + seconds
        self.state = "collecting"
        return self.status()

    def status(self):
        if self.state == "collecting" and self.clock.monotonic() >= self.deadline:
            self.state = "incomplete" if self.seen else "unsupported"
        return {
            "session": self.session,
            "state": self.state,
            "ids": list(self.ids),
            "rejected": self.rejected,
        }

    def cancel(self):
        if self.state in {"collecting", "complete"}:
            self.state = "cancelled"
        return self.status()

    def feed(self, event):
        if self.status()["state"] != "collecting":
            return
        if event.get("post_type") != "notice" or event.get("notice_type") != "group_msg_emoji_like":
            return
        self.seen += 1
        actor = event.get("user_id")
        likes = event.get("likes")
        valid = (
            identifier(actor)
            and event.get("is_add") is True
            and self.identity.resolve_actor(str(actor)).role == "owner"
        )
        values = (
            [
                str(row["emoji_id"])
                for row in likes
                if isinstance(row, dict)
                and identifier(row.get("emoji_id"))
                and len(str(row["emoji_id"])) <= 32
            ]
            if isinstance(likes, list)
            else []
        )
        if not valid or not values:
            self.rejected += 1
            return
        for value in values:
            if value not in self.ids:
                self.ids.append(value)
            if len(self.ids) == 4:
                self.state = "complete"
                break
