"""启动时固定的会话准入；仅保留按原因分类的拒绝计数。"""

from copy import deepcopy
from types import SimpleNamespace

REASONS = (
    "not_test_conversation",
    "not_registered",
    "stranger_private",
    "sender_not_expected",
    "unknown_kind",
)


class Admission:
    def __init__(self, config, instance):
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
        elif self.mode != "registered":
            return "not_registered"
        if conversation.kind == "group" and self.conversations.get(cid, {}).get("kind") != "group":
            return "not_registered"
        return None

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
