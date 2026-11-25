"""逐页分配画图接口的参考图名额。"""

from clytie.sdk.types import AssetRef


def page_references(
    panels: list[dict], portraits: dict[str, list[AssetRef]], style_page: AssetRef | None
) -> tuple[list[AssetRef], list[str]]:
    roles = list(dict.fromkeys(role for panel in panels for role in panel["characters"]))
    selected = [style_page] if style_page is not None else []
    represented = set()
    for index in range(max((len(portraits[role]) for role in roles), default=0)):
        for role in roles:
            if index < len(portraits[role]):
                selected.append(portraits[role][index])
                represented.add(role)
                if len(selected) == 8:
                    return selected, [role for role in roles if role not in represented]
    return selected, [role for role in roles if role not in represented]
