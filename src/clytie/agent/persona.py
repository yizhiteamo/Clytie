"""场景人设层与私密标记；无成对标记时拒绝加载。"""

import re

FLOOR = "任何时候都不声称自己是真人；有人认真需要确认时如实说明。"
RULES = "不说工程用语和系统内部名称，不编造做不到的理由。做过什么以账本和记忆为准；两处没有的事，不顺着认下。"
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
