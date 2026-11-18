"""Sequential OneBot driver. Retain only fields needed for the current case."""

import json
from uuid import uuid4

import aiohttp

from clytie.plugin_host.support import timed

from .config import SmokeError, require


class Driver:
    def __init__(self, target, clock):
        self.target = target
        self.clock = clock
        self.events = []

    async def connect(self, url, token):
        async def reject_redirect(*args):
            raise SmokeError("ws_redirect")

        trace = aiohttp.TraceConfig()
        trace.on_request_redirect.append(reject_redirect)
        self.session = aiohttp.ClientSession(trace_configs=[trace])
        headers = {}
        headers["Authorization"] = "Bearer " + token
        self.socket = await self.session.ws_connect(url, headers=headers)

    async def close(self):
        if hasattr(self, "session"):
            await self.session.close()

    def observe(self, d):
        if (
            str(d.get("group_id")) != self.target["group"]
            or str(d.get("user_id")) != self.target["bot"]
        ):
            return
        mid = str(d.get("message_id"))
        at = self.clock.monotonic()
        if d.get("post_type") in {"message", "message_sent"} and d.get("message_type") == "group":
            parts = d["message"]
            require(isinstance(parts, list), "invalid_message")
            chars = sum(len(p["data"]["text"]) for p in parts if p.get("type") == "text")
            self.events.append(
                (
                    "reply",
                    mid,
                    dict(chars=chars, image=any(p.get("type") == "image" for p in parts), at=at),
                )
            )
        elif (
            d.get("post_type") == "notice"
            and d.get("notice_type") == "group_msg_emoji_like"
            and type(d.get("is_add")) is bool
        ):
            for like in d.get("likes", []):
                state = self.target["emojis"].get(str(like.get("emoji_id")))
                if state:
                    self.events.append(("reaction", mid, (state, d["is_add"], at)))
        require(len(self.events) <= 2048, "event_overflow")

    async def read(self):
        frame = await self.socket.receive()
        require(frame.type == aiohttp.WSMsgType.TEXT, "ws_closed")
        data = json.loads(frame.data)
        self.observe(data)
        return data

    async def rpc(self, action, params):
        echo = uuid4().hex
        await self.socket.send_json(dict(action=action, params=params, echo=echo))

        async def response():
            while True:
                obj = await self.read()
                if obj.get("echo") == echo:
                    require(
                        obj.get("status") == "ok" and obj.get("retcode") == 0, "protocol_failed"
                    )
                    return obj["data"]

        return await timed(response(), self.clock, 10)

    async def authorize(self):
        members = await self.rpc(
            "get_group_member_list", {"group_id": self.target["group"], "no_cache": True}
        )
        actual = {str(m["user_id"]) for m in members}
        require(
            actual <= self.target["allowed"] and self.target["bot"] in actual, "member_mismatch"
        )
        login = str((await self.rpc("get_login_info", {}))["user_id"])
        require(login in self.target["testers"] and login in actual, "account_mismatch")
        return len(actual), self.target["testers"].index(login) + 1

    async def send(self, case):
        parts = (
            [{"type": "at", "data": {"qq": self.target["bot"]}}]
            if case.get("mention", True)
            else []
        )
        parts.append({"type": "text", "data": {"text": case["text"]}})
        self.events.clear()
        response = await self.rpc(
            "send_group_msg", {"group_id": self.target["group"], "message": parts}
        )
        require(
            type(response.get("message_id")) in {int, str} and str(response["message_id"]),
            "send_unknown",
        )
        return str(response["message_id"])
