"""场景人设层与私密标记；无成对标记时拒绝加载。"""

import re

FLOOR = "任何时候都不声称自己是真人；有人认真需要确认时如实说明。"
RULES = (
    "不说工程用语和系统内部名称，不编造做不到的理由。做过什么以账本和记忆为准；两处没有的事，不顺着认下。"
    "自然地用记忆，不说根据记录、我的记忆显示，不报日期和编号。很久以前的小事可以记得模糊一些；"
    "不确定时可以问，真不记得就直说，不编。人设描述的是你是谁、怎么说话和与主人的关系；"
    "具体发生过的共同经历只以记忆和任务记录为准，关系设定不等于具体发生过的事。"
    "记忆与聊天冲突时，以主人最近亲口说的为准，用 core_remember 的 replaces 修订旧记忆。"
    "主人说不用记、别记这个时不记；已经在本次记下的，用 core_forget 撤销。"
    "对方说起你们之间的事或说你答应过什么时，先看任务记录和记忆；都没有时用 core_recall 回想；"
    "仍没有就直说不记得，不顺着认下。"
)
MARKERS = re.compile(r"<!-- (/?private) -->")


class Persona:
    def __init__(self, root, config):
        self.config, self.layers = config, {}
        for layer, filename in config["persona"]["files"].items():
            path = (root / filename).resolve()
            if not path.is_relative_to(root.resolve()):
                raise ValueError("persona path outside directory")
            text = path.read_text(encoding="utf-8")
            private, offset, segments = False, 0, []
            for match in MARKERS.finditer(text):
                opening = match.group(1) == "private"
                if opening == private:
                    raise ValueError(
                        f"{path}:line {text.count(chr(10), 0, match.start()) + 1}: unpaired private marker"
                    )
                segments.append((private, text[offset : match.start()]))
                private, offset = opening, match.end()
            if private:
                raise ValueError(
                    f"{path}:line {text.count(chr(10), 0, offset) + 1}: unpaired private marker"
                )
            segments.append((False, text[offset:]))
            self.layers[layer] = segments

    def render(self, scene):
        private = scene in self.config.get("memory", {}).get("private_scenes", ["owner_private"])
        return "\n".join(
            [
                RULES,
                *[
                    text
                    for layer in self.config["persona"]["scenes"][scene]
                    for secret, text in self.layers[layer]
                    if private or not secret
                ],
                FLOOR,
            ]
        )
