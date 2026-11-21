"""Sequential OneBot driver. Retain only fields needed for the current case."""

import asyncio
import json
from collections import deque
from urllib.parse import urlsplit
from uuid import uuid4

import aiohttp

from clytie.sdk.smoke import SmokeDriverError, SmokeReaction, SmokeReply


def require(value, code):
    if not value:
        raise SmokeDriverError(code)


class Driver:
    def __init__(self, clock):
        self.target = None
        self.clock = clock
        self.pending = deque()
        self.case_id = None
        self.last_reply_id = None

    async def connect(self, config, secrets):
        url = config["ws_url"]
        parsed = urlsplit(url)
        require(
            parsed.scheme in {"ws", "wss"}
            and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
            and not parsed.username
            and not parsed.password,
            "invalid_ws",
        )
        token = secrets.read(config["token_file"])

        async def reject_redirect(*args):
            raise SmokeDriverError("ws_redirect")

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
            self.target is None
            or str(d.get("group_id")) != self.target.conversation
            or str(d.get("user_id")) != self.target.companion_account
        ):
            return
        mid = str(d.get("message_id"))
        at = self.clock.monotonic()
        if d.get("post_type") in {"message", "message_sent"} and d.get("message_type") == "group":
            parts = d["message"]
            require(isinstance(parts, list), "invalid_message")
            chars = sum(len(p["data"]["text"]) for p in parts if p.get("type") == "text")
            reply = SmokeReply(chars, any(p.get("type") == "image" for p in parts), at)
            raw_id = d.get("message_id")
            self.pending.append((reply, mid if type(raw_id) in {int, str} and mid else None))
        elif (
            d.get("post_type") == "notice"
            and d.get("notice_type") == "group_msg_emoji_like"
            and type(d.get("is_add")) is bool
        ):
            for like in d.get("likes", []):
                state = self.emojis.get(str(like.get("emoji_id")))
                if state:
                    self.pending.append((SmokeReaction(mid, state, d["is_add"], at), None))
        require(len(self.pending) <= 2048, "event_overflow")

    async def events(self):
        while True:
            while self.pending:
                event, reply_id = self.pending.popleft()
                if isinstance(event, SmokeReply):
                    self.last_reply_id = reply_id
                yield event
            await self.read()

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

        reply = asyncio.create_task(response())
        timer = asyncio.create_task(self.clock.sleep(10))
        try:
            done, _ = await asyncio.wait((reply, timer), return_when=asyncio.FIRST_COMPLETED)
            if reply in done:
                return reply.result()
            raise TimeoutError
        finally:
            reply.cancel()
            timer.cancel()
            await asyncio.gather(reply, timer, return_exceptions=True)

    async def authorize(self, target):
        self.target = target
        states = {symbol: state for state, symbol in target.reaction_symbols.items()}
        self.emojis = {
            str(number): states[symbol]
            for symbol, number in target.binding_config.get("emoji_ids", {}).items()
            if symbol in states
        }
        members = await self.rpc(
            "get_group_member_list", {"group_id": self.target.conversation, "no_cache": True}
        )
        actual = {str(m["user_id"]) for m in members}
        require(
            actual <= self.target.allowed_accounts and self.target.companion_account in actual,
            "member_mismatch",
        )
        login = str((await self.rpc("get_login_info", {}))["user_id"])
        require(login in self.target.tester_accounts and login in actual, "account_mismatch")
        return len(actual), self.target.tester_accounts.index(login) + 1

    async def send(self, case, image=None):
        parts = []
        if self.case_id != case["id"]:
            # Only observations consumed by the previous verdict can be quoted.
            self.pending.clear()
            if case.get("reply_to_last"):
                require(self.last_reply_id, "invalid_message")
                parts.append({"type": "reply", "data": {"id": self.last_reply_id}})
            self.case_id = case["id"]
            self.last_reply_id = None
        if case.get("mention", True):
            parts.append({"type": "at", "data": {"qq": self.target.companion_account}})
        parts.append({"type": "text", "data": {"text": case["text"]}})
        if image is not None:
            import base64

            parts.append(
                {
                    "type": "image",
                    "data": {"file": "base64://" + base64.b64encode(image).decode("ascii")},
                }
            )
        response = await self.rpc(
            "send_group_msg", {"group_id": self.target.conversation, "message": parts}
        )
        require(
            type(response.get("message_id")) in {int, str} and str(response["message_id"]),
            "send_unknown",
        )
        return str(response["message_id"])
