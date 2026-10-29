"""从已校验实例映射构造每个账号的逻辑身份视图，不读取凭据。"""

import hashlib

from clytie.sdk.types import Actor, Conversation


class IdentityResolver:
    def __init__(self, channel_id, plugin_id, instance):
        self._channel = channel_id
        self._actors, self._conversations = {}, {}
        for actor_id, person in instance.get("people", {}).items():
            if actor_id.startswith("x-"):
                continue
            for account in person["accounts"]:
                if account["channel"] == plugin_id:
                    self._actors[account["account"]] = Actor(
                        actor_id=actor_id, role=person["role"], channel_id=channel_id
                    )
        for cid, conversation in instance.get("conversations", {}).items():
            if cid.startswith("x-") or conversation["channel"] != plugin_id:
                continue
            self._conversations[conversation["kind"], conversation["platform_id"]] = Conversation(
                conversation_id=cid,
                kind=conversation["kind"],
                scene=conversation["scene"],
                channel_id=channel_id,
            )

    def resolve_actor(self, account):
        if account in self._actors:
            return self._actors[account]
        digest = hashlib.sha256(account.encode()).hexdigest()[:16]
        return Actor(
            actor_id=f"anon:{self._channel}:{digest}", role="stranger", channel_id=self._channel
        )

    def resolve_conversation(self, kind, platform_id):
        if (kind, platform_id) in self._conversations:
            return self._conversations[kind, platform_id]
        digest = hashlib.sha256(platform_id.encode()).hexdigest()[:16]
        return Conversation(
            conversation_id=f"anon:{self._channel}:{digest}",
            kind=kind,
            scene="public_group" if kind == "group" else "stranger_private",
            channel_id=self._channel,
        )

    def account_for(self, actor_id):
        return next(
            (account for account, actor in self._actors.items() if actor.actor_id == actor_id), None
        )

    def platform_id_for(self, conversation_id):
        return next(
            (
                key[1]
                for key, conversation in self._conversations.items()
                if conversation.conversation_id == conversation_id
            ),
            None,
        )
