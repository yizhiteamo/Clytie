import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml


class SmokeError(Exception):
    """Fixed, report-safe failure code."""


def require(value, code):
    if not value:
        raise SmokeError(code)


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8-sig"))


def ident(value):
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z][A-Za-z_0-9-]{0,63}", value)
        and not re.search(r"\d{5}", value)
    )


def target_config(home, cid, tester):
    instance = load(home / "clytie.local.yaml")
    require(ident(cid) and cid in instance["companions"], "invalid_companion")
    config = load(home / "companions" / cid / "companion.yaml")
    (binding,) = [b for b in config["channels"] if b["plugin"] == "qq-onebot"]
    bot = str(instance["accounts"][binding["account"]]["account"])
    group = instance["conversations"][instance["test_conversation"]]
    require(group["kind"] == "group" and group["channel"] == "qq-onebot", "invalid_target")

    def accounts(person):
        return [str(a["account"]) for a in person["accounts"] if a["channel"] == "qq-onebot"]

    person = instance["people"][tester]
    require(person["role"] == "member" and tester in group["members_expected"], "invalid_tester")
    allowed = set()
    for member in group["members_expected"]:
        if member in instance["people"]:
            allowed.update(accounts(instance["people"][member]))
        elif member in instance["accounts"]:
            a = instance["accounts"][member]
            require(a["channel"] == "qq-onebot", "invalid_target")
            allowed.add(str(a["account"]))
        elif member == cid:
            allowed.add(bot)
        else:
            raise SmokeError("unmapped_member")
    testers = accounts(person)
    owners = {a for p in instance["people"].values() if p["role"] == "owner" for a in accounts(p)}
    require(testers and not set(testers) & (owners | {bot}), "invalid_tester")
    reactions = config.get("task_reactions", {})
    symbols = {
        "accepted": "👌",
        "running": "⏳",
        "done": "✅",
        "failed": "❌",
        **reactions.get("emojis", {}),
    }
    states = {symbol: state for state, symbol in symbols.items()}
    emojis = {
        str(v): states[k] for k, v in binding["config"].get("emoji_ids", {}).items() if k in states
    }
    return dict(
        group=str(group["platform_id"]),
        bot=bot,
        testers=testers,
        allowed=allowed,
        emojis=emojis,
        interval=reactions.get("min_interval_seconds", 2),
    )


def prepare(home):
    cfg = load(home / "smoke/smoke.yaml")
    cases = load(home / "smoke/cases.yaml")["cases"]
    url = urlsplit(cfg["ws_url"])
    require(
        url.scheme in {"ws", "wss"}
        and url.hostname in {"127.0.0.1", "::1", "localhost"}
        and not url.username
        and not url.password,
        "invalid_ws",
    )
    token = Path(cfg["token_file"])
    require(
        not token.is_absolute()
        and len(token.parts) == 2
        and token.parts[0] == "secrets"
        and (home / token).resolve().parent == home.resolve() / "secrets",
        "invalid_token_file",
    )
    limits = cfg.get("limits", {})
    caps = [limits.get("max_cases", 10), limits.get("max_images", 4)]
    require(all(type(v) is int and v >= 0 for v in caps), "invalid_limits")
    require(isinstance(cases, list) and 0 < len(cases) <= caps[0], "case_limit")
    seen = set()
    for c in cases:
        e = c["expect"]
        require(
            ident(c["id"]) and c["id"] not in seen and isinstance(c["text"], str) and c["text"],
            "invalid_case",
        )
        seen.add(c["id"])
        require(
            all(type(x) is bool for x in (e["reply"], e["image"], c.get("mention", True)))
            and type(c["timeout_s"]) is int
            and c["timeout_s"] > 0,
            "invalid_case",
        )
        states = e["reactions"]
        require(
            states == "none"
            or isinstance(states, list)
            and states
            and all(
                isinstance(s, str)
                and s.removeprefix("optional:") in {"accepted", "running", "done", "failed"}
                for s in states
            )
            and not states[-1].startswith("optional:"),
            "invalid_reactions",
        )
    require(sum(c["expect"]["image"] for c in cases) <= caps[1], "image_limit")
    return cfg, cases, target_config(home, cfg["companion"], cfg["tester"])
