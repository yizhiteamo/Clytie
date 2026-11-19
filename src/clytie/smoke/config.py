import re
from pathlib import Path

import yaml

from clytie.plugin_host.snapshots import SnapshotError, contained
from clytie.sdk.immutable import freeze
from clytie.sdk.smoke import SmokeTarget


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
    group = instance["conversations"][instance["test_conversation"]]
    channel = group["channel"]
    require(group["kind"] == "group", "invalid_target")
    bindings = [b for b in config["channels"] if b["plugin"] == channel]
    require(len(bindings) == 1, "invalid_target")
    binding = bindings[0]
    account = instance["accounts"][binding["account"]]
    require(account["channel"] == channel, "invalid_target")
    bot = str(account["account"])

    def accounts(person):
        return [str(a["account"]) for a in person["accounts"] if a["channel"] == channel]

    person = instance["people"][tester]
    require(person["role"] == "member" and tester in group["members_expected"], "invalid_tester")
    allowed = set()
    for member in group["members_expected"]:
        if member in instance["people"]:
            allowed.update(accounts(instance["people"][member]))
        elif member in instance["accounts"]:
            a = instance["accounts"][member]
            require(a["channel"] == channel, "invalid_target")
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
    return SmokeTarget(
        channel=channel,
        conversation=str(group["platform_id"]),
        companion_account=bot,
        tester_accounts=tuple(testers),
        allowed_accounts=frozenset(allowed),
        binding_config=freeze(binding.get("config", {})),
        reaction_symbols=freeze(symbols),
        min_interval_seconds=reactions.get("min_interval_seconds", 2),
    )


class ConfiguredSecrets:
    def __init__(self, home, config):
        self._home = home.resolve()
        self._token_file = config.get("token_file")

    def read(self, path):
        require(isinstance(path, str) and path == self._token_file, "invalid_token_file")
        token = Path(path)
        require(
            not token.is_absolute()
            and len(token.parts) == 2
            and token.parts[0] == "secrets"
            and (self._home / token).parent == self._home / "secrets",
            "invalid_token_file",
        )
        try:
            selected = contained(self._home, self._home / token)
        except (OSError, SnapshotError) as exc:
            raise SmokeError("invalid_token_file") from exc
        require(selected.is_file(), "invalid_token_file")
        value = selected.read_text(encoding="utf-8").strip()
        require(value, "missing_secret")
        return value


def case_image(home, case):
    if "image" not in case:
        return None
    name = case["image"]
    require(
        isinstance(name, str)
        and name
        and not Path(name).anchor
        and ".." not in Path(name).parts
        and ":" not in name
        and "\\" not in name,
        "invalid_image",
    )
    root = home / "smoke"
    try:
        path = contained(home, contained(root, root / name))
        require(path.is_file() and path.stat().st_nlink == 1, "invalid_image")
        with path.open("rb") as stream:
            data = stream.read(20 * 1024 * 1024 + 1)
        require(
            len(data) <= 20 * 1024 * 1024
            and (
                data.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a"))
                or data.startswith(b"RIFF")
                and data[8:12] == b"WEBP"
            ),
            "invalid_image",
        )
        return data
    except (OSError, SnapshotError) as exc:
        raise SmokeError("invalid_image") from exc


def prepare(home):
    cfg = load(home / "smoke/smoke.yaml")
    cases = load(home / "smoke/cases.yaml")["cases"]
    driver = cfg.get("driver")
    require(
        isinstance(driver, dict)
        and ident(driver.get("plugin"))
        and isinstance(driver.get("config"), dict),
        "invalid_driver",
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
    target = target_config(home, cfg["companion"], cfg["tester"])
    require(target.channel == driver["plugin"], "driver_channel_mismatch")
    return cfg, cases, target
