"""通过当前已核验的连接逐页读取指定会话。"""

import asyncio

from clytie.sdk import ChannelUnavailable

from .mapping import _number, normalize_event


async def _page_interval(clock, cancel):
    delay = asyncio.create_task(clock.sleep(1))
    stopped = asyncio.create_task(cancel.wait())
    try:
        await asyncio.wait((delay, stopped), return_when=asyncio.FIRST_COMPLETED)
        cancel.raise_if_cancelled()
    finally:
        delay.cancel()
        stopped.cancel()
        await asyncio.gather(delay, stopped, return_exceptions=True)


async def read_history(channel, conversation_id, after, limit, cancel):
    cancel.raise_if_cancelled()
    if not channel.features().supports_history:
        raise ChannelUnavailable("unsupported")
    ctx = channel.ctx
    peer = ctx.identity.platform_id_for(conversation_id)
    if peer is None:
        raise ChannelUnavailable("unknown_conversation")
    group = ctx.identity.resolve_conversation("group", peer).conversation_id == conversation_id
    kind = "group" if group else "private"
    if ctx.identity.resolve_conversation(kind, peer).conversation_id != conversation_id:
        raise ChannelUnavailable("unknown_conversation")
    action = "get_group_msg_history" if group else "get_friend_msg_history"
    size = ctx.config.get("history_page_size", 50)
    connection = channel._ws
    cursor, used = "0", set()
    events = {}
    while len(events) < limit and cursor not in used:
        try:
            if used:
                await _page_interval(ctx.clock, cancel)
            if channel.state().status != "online" or channel._ws is not connection:
                raise ChannelUnavailable("offline")
            reply = await channel._request(
                action,
                {
                    "group_id" if group else "user_id": peer,
                    "message_seq": cursor,
                    "reverse_order": False,
                    "count": size,
                },
                cancel,
            )
            channel._log_result(action, reply)
            if (
                isinstance(reply, dict)
                and reply.get("status") == "failed"
                and reply.get("retcode") == 1404
            ):
                if channel._ws is connection:
                    channel._history_enabled = False
                raise ChannelUnavailable("unsupported")
            data = (
                reply.get("data")
                if isinstance(reply, dict) and reply.get("status") == "ok"
                else None
            )
            page = data.get("messages") if isinstance(data, dict) else None
            if not isinstance(page, list):
                raise ChannelUnavailable("history_failed")
        except (ChannelUnavailable, asyncio.CancelledError):
            if not used:
                raise
            break
        used.add(cursor)
        positions = []
        malformed = 0
        for row in page:
            if not isinstance(row, dict) or row.get("message_type") != kind:
                malformed += 1
                continue
            timestamp = row.get("time")
            identifier = _number(row.get("message_id"), signed=True)
            if type(timestamp) is not int or identifier is None:
                malformed += 1
                continue
            positions.append((timestamp, identifier))
            if str(row.get("user_id")) == channel._expected:
                continue
            event, count = normalize_event(
                ctx, {"self_id": channel._expected, **row}, channel._expected
            )
            if (
                not event
                or not event.message
                or event.conversation.conversation_id != conversation_id
            ):
                malformed += 1
                continue
            malformed += count
            if event.message.sent_at > after:
                events.setdefault(event.event_id, event)
        if malformed:
            channel._counts["malformed"] += malformed
            await channel._publish()
        if len(page) < size or not positions:
            break
        oldest, cursor = min(positions, key=lambda item: item[0])
        if oldest <= after.timestamp():
            break
    return sorted(events.values(), key=lambda event: event.message.sent_at)[-limit:]
