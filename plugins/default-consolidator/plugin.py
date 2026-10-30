"""本体选窗口、核对和提交；插件仅生成有出处的提议。"""

import json
from datetime import timedelta

from clytie.sdk.types import Budget, ChatMessage, ModelRequest, RecollectItem, TextPart

RULES = """回想刚才的对话，只返回 JSON 数组，每项包含 kind、content、visibility、evidence（消息编号）、可选 about（说话人标签）、merges（记忆标签）、with_owner。
每条有明确出处；没有值得记的就返回空数组。不补编细节。主人说不用记、别记这个的内容不记。
群聊只能 episode/profile/jargon，别人的话只记为某人说过，不当作关于主人的事实。
profile 只写实际说过话的群友的公开无害印象，最多两百字，不给主人建印象；其他内容最多三百字。
relationship 是可以在别人面前提的共同经历，private 是不能对外说的秘密，public 是谁都可知道；拿不准用 private。
不合并主人说的记录，不输出模型工具调用。"""


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx

    def create_consolidator(self):
        return self

    async def stop(self):
        self.ctx = None

    async def run(self, window, reader, cancel):
        rows = []
        async for batch in reader.iter():
            rows.extend(batch)
        candidates = [
            {"label": f"[记忆{i}]", "kind": r.kind, "content": r.content, "basis": r.basis}
            for i, r in enumerate(rows, 1)
        ]
        request = ModelRequest(
            messages=[
                ChatMessage(role="system", parts=[TextPart(text=RULES)]),
                ChatMessage(
                    role="user",
                    parts=[
                        TextPart(
                            text=json.dumps(
                                {
                                    "window": window.model_dump(mode="json"),
                                    "candidates": candidates,
                                },
                                ensure_ascii=False,
                            )
                        )
                    ],
                ),
            ],
            budget=Budget(deadline=self.ctx.clock.now() + timedelta(seconds=60), max_tool_rounds=0),
            trace_id=f"{self.ctx.random.getrandbits(128):032x}",
        )
        turn = await self.ctx.model.generate(request, cancel, purpose="memory.recollect")
        cancel.raise_if_cancelled()
        if turn.finish != "stop":
            raise ValueError("recollection generation failed")
        result = json.loads(turn.text or "[]")
        if not isinstance(result, list):
            raise ValueError("recollection must return a list")
        return [RecollectItem.model_validate(item) for item in result]
