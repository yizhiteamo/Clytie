"""任务单纯格式化；名称来自伴侣配置，出站检查仍由 A3.2 接入。"""

import math

DEFAULT = "【{label} {id} · {name} · 协助：{helper} · 用时约{duration}】"


def name_for(config, scene):
    names = config["names"]
    return (
        names["internal"]
        if scene in names.get("internal_scenes", ["owner_private"])
        else names.get("external", {}).get(scene, names["internal"])
    )


def render_task_card(config, task_id, seconds, *, scene, helper=None):
    kind = (
        "internal"
        if scene in config["names"].get("internal_scenes", ["owner_private"])
        else "external"
    )
    card = config.get("task_card", {})
    template = card.get(kind, DEFAULT)
    if helper is None:
        template = "·".join(part for part in template.split("·") if "{helper}" not in part)
    seconds = math.floor(max(0, seconds) + 0.5)
    duration = f"{seconds} 秒" if seconds <= 60 else f"{seconds / 60:.1f} 分钟"
    return template.format(
        label=card.get("label", {}).get(kind, "任务"),
        id=task_id,
        name=name_for(config, scene),
        helper=name_for(helper, scene) if helper else "",
        duration=duration,
    )
