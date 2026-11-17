"""启动时固定的会话准入；仅保留按原因分类的拒绝计数。"""

from copy import deepcopy
from types import SimpleNamespace

REASONS = (
    "not_test_conversation",
    "not_registered",
    "stranger_private",
    "sender_not_expected",
    "unknown_kind",
    "owner_absent",
    "membership_unknown",
)


class Admission:
    def __init__(self, config, instance, *, channels=None, clock=None):
        from clytie.clock import SystemClock

        self.channels = channels if channels is not None else {}
        self.clock = clock or SystemClock()
        self.membership = {}
        self.observed_groups = set()
        self.generations = {}
        self.owner = next(
            (
                key
                for key, value in instance.get("people", {}).items()
                if not key.startswith("x-")
                and isinstance(value, dict)
                and value.get("role") == "owner"
            ),
            None,
        )
        self.mode = config.get("boundary", {}).get("admission", "registered")
        self.test = instance.get("test_conversation")
        self.conversations = {
            cid: deepcopy(value)
            for cid, value in instance.get("conversations", {}).items()
            if not cid.startswith("x-")
        }
        self.counts = dict.fromkeys(REASONS, 0)
        self.private_actors = {}
        for cid, conversation in self.conversations.items():
            if conversation.get("kind") != "private":
                continue
            for actor_id, person in instance.get("people", {}).items():
                if actor_id.startswith("x-"):
                    continue
                if any(
                    a.get("channel") == conversation.get("channel")
                    and a.get("account") == conversation.get("platform_id")
                    for a in person.get("accounts", ())
                ):
                    self.private_actors[cid] = SimpleNamespace(
                        actor_id=actor_id, role=person.get("role")
                    )

    def reason(self, conversation, sender):
        if conversation.kind not in {"private", "group"}:
            return "unknown_kind"
        if conversation.kind == "private" and (
            sender is None or sender.role not in {"owner", "member"}
        ):
            return "stranger_private"
        cid = conversation.conversation_id
        if self.mode == "test_only":
            if cid != self.test or cid not in self.conversations:
                return "not_test_conversation"
            if sender is None or sender.actor_id not in self.conversations[cid].get(
                "members_expected", ()
            ):
                return "sender_not_expected"
        elif self.mode not in {"registered", "owner_present"}:
            return "not_registered"
        if conversation.kind == "group" and self.conversations.get(cid, {}).get("kind") != "group":
            return "not_registered"
        return None

    def invalidate(self, channel_id, conversation_id):
        key = (channel_id, conversation_id)
        self.membership.pop(key, None)
        self.generations[key] = self.generations.get(key, 0) + 1

    async def checked_reason(self, conversation, sender):
        reason = self.reason(conversation, sender)
        if (
            self.mode != "owner_present"
            or reason != "not_registered"
            or conversation.kind != "group"
        ):
            return reason
        key = (conversation.channel_id, conversation.conversation_id)
        self.observed_groups.add(key)
        channel = self.channels.get(conversation.channel_id)
        try:
            supported = channel is not None and channel.features().supports_membership
        except Exception:
            supported = False
        if not supported or self.owner is None:
            return "membership_unknown"
        cached = self.membership.get(key)
        if cached is not None and self.clock.monotonic() < cached[0]:
            result = cached[1]
        else:
            from clytie.plugin_host.support import timed

            generation = self.generations.get(key, 0)
            try:
                result = await timed(
                    channel.is_member(conversation.conversation_id, self.owner), self.clock, 5
                )
                if result is not True and result is not False:
                    result = None
            except Exception:
                result = None
            if self.generations.get(key, 0) != generation:
                return "membership_unknown"
            if result is not None:
                self.membership[key] = (self.clock.monotonic() + 600, result)
        return (
            None if result is True else "owner_absent" if result is False else "membership_unknown"
        )

    async def accept_checked(self, event):
        if getattr(event, "type", None) == "conversation.notice" and event.notice.kind in {
            "member_join",
            "member_leave",
        }:
            self.invalidate(event.channel_id, event.conversation.conversation_id)
            return False
        reason = await self.checked_reason(
            event.conversation, event.message.sender if event.message else None
        )
        if reason:
            self.counts[reason] += 1
        return reason is None

    async def allows_delivery_checked(self, intent, inbound):
        if self.allows_delivery(intent, inbound):
            return True
        if self.mode != "owner_present":
            return False
        if not inbound:
            if (intent.channel_id, intent.conversation_id) not in self.observed_groups:
                return False
            return (
                await self.checked_reason(
                    SimpleNamespace(
                        kind="group",
                        channel_id=intent.channel_id,
                        conversation_id=intent.conversation_id,
                    ),
                    None,
                )
                is None
            )
        source = inbound.get("conversation", {})
        if source.get("conversation_id") != intent.conversation_id:
            return False
        actor = (inbound.get("message") or {}).get("sender")
        sender = SimpleNamespace(**actor) if actor else None
        return (
            await self.checked_reason(
                SimpleNamespace(
                    kind=source.get("kind"),
                    conversation_id=intent.conversation_id,
                    channel_id=intent.channel_id,
                ),
                sender,
            )
            is None
        )

    def accept(self, event):
        sender = event.message.sender if event.message else None
        reason = self.reason(event.conversation, sender)
        if reason:
            self.counts[reason] += 1
        return reason is None

    def allows_delivery(self, intent, inbound):
        target = self.conversations.get(intent.conversation_id)
        sender = None
        if inbound:
            source = inbound.get("conversation", {})
            if source.get("conversation_id") != intent.conversation_id:
                return False
            kind = source.get("kind")
            actor = (inbound.get("message") or {}).get("sender")
            if actor:
                sender = SimpleNamespace(role=actor.get("role"), actor_id=actor.get("actor_id"))
        else:
            kind = target.get("kind") if target else None
            sender = self.private_actors.get(intent.conversation_id)
            # 主动群发没有发言人，test_only 的成员限制只针对入站发言人。
            if kind == "group" and self.mode == "test_only":
                return intent.conversation_id == self.test
        return (
            self.reason(SimpleNamespace(kind=kind, conversation_id=intent.conversation_id), sender)
            is None
        )

    def status(self):
        return {"mode": self.mode, "rejected": dict(self.counts)}
