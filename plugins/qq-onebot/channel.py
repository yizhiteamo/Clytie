"""OneBot 正向连接、请求对应和有界入站队列。"""

import asyncio
import json

from clytie.sdk import CancelToken, ChannelUnavailable, CredentialUnavailable
from clytie.sdk.immutable import thaw
from clytie.sdk.types import ChannelFeatures, ChannelState, ErrorInfo, Receipt

from .mapping import encode_parts, normalize_event


class OneBotChannel:
    def __init__(self, ctx):
        self.ctx = ctx
        self._config = ctx.config
        self._expected = ctx.identity.self_account()
        self._channel_id = (
            ctx.identity.resolve_actor(self._expected).channel_id
            if self._expected is not None
            else None
        )
        self._state = ChannelState(status="offline", since=ctx.clock.now())
        self._counts = {"self_id_mismatch": 0, "queue_overflow": 0, "malformed": 0}
        self._queue = asyncio.Queue(maxsize=self._config.get("queue_limit", 128))
        self._pending = {}
        self._sequence = 0
        self._ws = self._reader = self._heartbeat = self._manager = None
        self._opening = None
        self._stop = asyncio.Event()
        self._cancel = CancelToken()
        self._auth_invalid = False
        self._reaction_enabled = self._configured_reactions()
        self._last_heartbeat = ctx.clock.monotonic()

    def _configured_reactions(self):
        ids = self._config.get("emoji_ids", {})
        return all(
            isinstance(ids.get(emoji), (str, int)) and str(ids[emoji]) for emoji in "👌⏳✅❌"
        )

    def state(self):
        return self._state

    async def _publish(self, status=None, code=None):
        if status is not None:
            self._state = ChannelState(status=status, since=self.ctx.clock.now(), code=code)
        await self.ctx.state.set(
            {"status": self._state.status, "code": self._state.code, **self._counts}
        )

    def features(self):
        return ChannelFeatures(
            max_text_chars=self._config.get("max_text_chars", 4000),
            supports_react=self._reaction_enabled,
            supports_quote=True,
            supports_card=False,
            media_limits=thaw(self._config.get("media_limits", {})),
        )

    async def connect(self):
        await self.ctx.clock.sleep(0)
        if self._auth_invalid:
            raise ChannelUnavailable("auth_invalid")
        if self._manager is not None and not self._manager.done():
            return
        self._stop.clear()
        self._cancel = CancelToken()
        self._opening = asyncio.current_task()
        try:
            await self._open()
        except asyncio.CancelledError:
            await self._disconnect()
            raise
        except ChannelUnavailable:
            await self._disconnect()
            raise
        except Exception:
            await self._disconnect()
            await self._publish("offline")
        finally:
            self._opening = None
        self._manager = asyncio.create_task(self._maintain())

    async def _open(self):
        await self._publish("connecting")
        try:
            credential_value = await self.ctx.credentials.account_secret()
        except (CredentialUnavailable, AttributeError):
            await self._invalid_auth()
        if self._expected is None or not credential_value:
            await self._invalid_auth()
        headers = {}
        headers["Authorization"] = "Bearer " + credential_value
        self._ws = await self.ctx.websocket.connect(
            self._config["url"],
            headers=headers,
            timeout_s=self._config.get("connect_timeout_s", 10),
            max_message_bytes=self._config.get("max_message_bytes", 16 * 1024 * 1024),
        )
        self._reader = asyncio.create_task(self._read(self._ws))
        result = await self._request("get_login_info", {}, self._cancel)
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise ConnectionError("login unavailable")
        data = result.get("data")
        if not isinstance(data, dict) or str(data.get("user_id")) != self._expected:
            await self._invalid_auth()
        self._reaction_enabled = self._configured_reactions()
        self._last_heartbeat = self.ctx.clock.monotonic()
        await self._publish("online")
        self._heartbeat = asyncio.create_task(self._watch_heartbeat(self._ws))

    async def _invalid_auth(self):
        self._auth_invalid = True
        await self._publish("auth_invalid", "auth_invalid")
        raise ChannelUnavailable("auth_invalid")

    async def _maintain(self):
        try:
            while not self._stop.is_set():
                if self._reader is not None:
                    await self._reader
                await self._disconnect()
                if self._stop.is_set():
                    return
                if self._state.status != "unknown":
                    await self._publish("offline")
                for delay in self._config.get("reconnect_delays", (1, 2, 4)):
                    await self.ctx.clock.sleep(delay)
                    if self._stop.is_set():
                        return
                    try:
                        await self._open()
                    except ChannelUnavailable:
                        await self._disconnect()
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        await self._disconnect()
                        continue
                    break
                else:
                    await self._publish("offline")
                    return
        except asyncio.CancelledError:
            raise
        finally:
            await self._disconnect()

    async def _watch_heartbeat(self, ws):
        timeout = self._config.get("heartbeat_timeout_s", 60)
        while not self._stop.is_set():
            remaining = timeout - (self.ctx.clock.monotonic() - self._last_heartbeat)
            if remaining <= 0:
                await self._publish("unknown", "heartbeat_timeout")
                await ws.close()
                return
            await self.ctx.clock.sleep(remaining)

    async def _read(self, ws):
        try:
            while not self._stop.is_set():
                raw = await ws.receive(self._cancel)
                try:
                    payload = json.loads(raw)
                except (ValueError, TypeError):
                    self._counts["malformed"] += 1
                    await self._publish()
                    continue
                if not isinstance(payload, dict):
                    self._counts["malformed"] += 1
                    await self._publish()
                    continue
                if "echo" in payload and "post_type" not in payload:
                    echo = payload["echo"]
                    future = self._pending.get(echo) if isinstance(echo, str) else None
                    if future is not None and not future.done():
                        future.set_result(payload)
                    continue
                if str(payload.get("self_id")) != self._expected:
                    self._counts["self_id_mismatch"] += 1
                    await self._publish()
                    continue
                if (
                    payload.get("post_type") == "meta_event"
                    and payload.get("meta_event_type") == "heartbeat"
                ):
                    self._last_heartbeat = self.ctx.clock.monotonic()
                    continue
                if self._state.status != "online":
                    continue
                event, malformed = normalize_event(self.ctx, payload, self._expected)
                self._counts["malformed"] += malformed
                if event is not None:
                    try:
                        self._queue.put_nowait(event)
                    except asyncio.QueueFull:
                        self._counts["queue_overflow"] += 1
                        self.ctx.log.warning(
                            "qq.queue_overflow", count=self._counts["queue_overflow"]
                        )
                if malformed or event is not None:
                    await self._publish()
        except asyncio.CancelledError:
            raise
        except Exception:
            pass
        finally:
            for future in tuple(self._pending.values()):
                if not future.done():
                    future.set_result(None)

    async def _request(self, action, params, cancel):
        cancel.raise_if_cancelled()
        ws = self._ws
        if ws is None or ws.closed:
            return None
        self._sequence += 1
        echo = str(self._sequence)
        future = asyncio.get_running_loop().create_future()
        self._pending[echo] = future

        async def exchange():
            await ws.send_text(
                json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
            )
            return await future

        operation = asyncio.create_task(exchange())
        timer = asyncio.create_task(self.ctx.clock.sleep(self._config.get("request_timeout_s", 10)))
        cancelled = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait(
                (operation, timer, cancelled), return_when=asyncio.FIRST_COMPLETED
            )
            cancel.raise_if_cancelled()
            result = await operation if operation in done else None
            return result
        except asyncio.CancelledError:
            raise
        except Exception:
            return None
        finally:
            self._pending.pop(echo, None)
            for task in (operation, timer, cancelled):
                task.cancel()
            await asyncio.gather(operation, timer, cancelled, return_exceptions=True)

    def _receipt(self, intent, status, code=None, message_id=None):
        return Receipt(
            intent_id=intent.intent_id,
            status=status,
            at=self.ctx.clock.now(),
            platform_message_id=message_id,
            error=ErrorInfo(code=code, message=code) if code else None,
        )

    def _log_result(self, action, result):
        status = result.get("status") if isinstance(result, dict) else None
        retcode = result.get("retcode") if isinstance(result, dict) else None
        self.ctx.log.info(
            "qq.action",
            action=action,
            status=status if status in ("ok", "failed", "async") else None,
            retcode=retcode if type(retcode) is int else None,
        )

    async def send(self, intent, cancel):
        cancel.raise_if_cancelled()
        await self.ctx.clock.sleep(0)
        if self._state.status != "online":
            return self._receipt(intent, "unknown")
        platform_id = self.ctx.identity.platform_id_for(intent.conversation_id)
        if intent.channel_id != self._channel_id or platform_id is None:
            return self._receipt(intent, "failed", "invalid_request")
        kind = next(
            (
                kind
                for kind in ("group", "private")
                if self.ctx.identity.resolve_conversation(kind, platform_id).conversation_id
                == intent.conversation_id
            ),
            None,
        )
        if kind is None:
            return self._receipt(intent, "failed", "invalid_request")
        try:
            parts = await encode_parts(self.ctx, intent, self.features().media_limits)
        except ValueError as error:
            code = (
                str(error)
                if str(error) in {"invalid_request", "media_limit"}
                else "invalid_request"
            )
            return self._receipt(intent, "failed", code)
        cancel.raise_if_cancelled()
        action = f"send_{kind}_msg"
        result = await self._request(
            action,
            {"group_id" if kind == "group" else "user_id": platform_id, "message": parts},
            cancel,
        )
        self._log_result(action, result)
        if (
            isinstance(result, dict)
            and result.get("status") == "failed"
            and type(result.get("retcode")) is int
        ):
            retcode = result.get("retcode")
            code = self._config.get("retcode_classes", {}).get(str(retcode), "permanent")
            return self._receipt(intent, "failed", code)
        if isinstance(result, dict) and result.get("status") == "ok":
            data = result.get("data")
            message_id = data.get("message_id") if isinstance(data, dict) else None
            if type(message_id) in (str, int) and str(message_id):
                return self._receipt(intent, "sent", message_id=str(message_id))
        return self._receipt(intent, "unknown")

    async def react(self, intent, cancel):
        cancel.raise_if_cancelled()
        await self.ctx.clock.sleep(0)
        if self._state.status != "online":
            return self._receipt(intent, "unknown")
        if not self._reaction_enabled or intent.emoji not in self._config.get("emoji_ids", {}):
            return self._receipt(intent, "failed", "unsupported")
        if (
            intent.channel_id != self._channel_id
            or intent.target is None
            or intent.target.channel_id != self._channel_id
            or intent.target.conversation_id != intent.conversation_id
        ):
            return self._receipt(intent, "failed", "invalid_request")
        result = await self._request(
            "set_msg_emoji_like",
            {
                "message_id": intent.target.platform_message_id,
                "emoji_id": self._config["emoji_ids"][intent.emoji],
            },
            cancel,
        )
        self._log_result("set_msg_emoji_like", result)
        if isinstance(result, dict) and result.get("status") == "ok":
            return self._receipt(intent, "sent", message_id=intent.target.platform_message_id)
        if (
            isinstance(result, dict)
            and result.get("status") == "failed"
            and result.get("retcode") == 1404
        ):
            self._reaction_enabled = False
            self.ctx.log.warning(
                "qq.reaction_unsupported", action="set_msg_emoji_like", retcode=1404
            )
        return self._receipt(intent, "unknown")

    async def events(self):
        while not self._stop.is_set():
            read = asyncio.create_task(self._queue.get())
            stopped = asyncio.create_task(self._stop.wait())
            try:
                await asyncio.wait((read, stopped), return_when=asyncio.FIRST_COMPLETED)
                if self._stop.is_set():
                    return
                yield await read
            finally:
                read.cancel()
                stopped.cancel()
                await asyncio.gather(read, stopped, return_exceptions=True)

    async def _disconnect(self):
        tasks = tuple(task for task in (self._heartbeat, self._reader) if task is not None)
        self._heartbeat = self._reader = None
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def close(self):
        await self.ctx.clock.sleep(0)
        self._stop.set()
        self._cancel.cancel()
        if self._opening is not None and self._opening is not asyncio.current_task():
            self._opening.cancel()
            await asyncio.gather(self._opening, return_exceptions=True)
        if self._manager is not None:
            self._manager.cancel()
            await asyncio.gather(self._manager, return_exceptions=True)
            self._manager = None
        await self._disconnect()
        if not self._auth_invalid:
            await self._publish("offline")
