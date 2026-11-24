"""公开新闻服务。材料逐件保存，结束后只留下去重和升级所需的标记。"""

import hashlib
import json
from datetime import datetime
from urllib.parse import urlsplit

from .schedule import DAY

WEEK = 7 * DAY
STORAGE_LIMIT = 1024 * 1024


def parse_offer(args):
    key, tier = args["key"], args["tier"]
    if not isinstance(key, str) or not key or tier not in ("brief", "breaking"):
        raise ValueError("invalid news offer")
    sources = []
    for source in args["sources"]:
        name, url = source["name"], source["url"]
        address = urlsplit(url)
        if not isinstance(name, str) or not name or address.scheme not in {"https", "http"}:
            raise ValueError("invalid news source")
        if not address.hostname or address.username is not None or address.password is not None:
            raise ValueError("invalid news source")
        sources.append({"name": name, "url": url})
    title, summary = args["title"], args["summary"]
    if not sources or not isinstance(title, str) or not title or not isinstance(summary, str):
        raise ValueError("invalid news material")
    at = datetime.fromisoformat(args["time"].replace("Z", "+00:00"))
    if at.tzinfo is None:
        raise ValueError("missing news time zone")
    public = {"title": title, "summary": summary, "sources": sources, "time": at.isoformat()}
    return hashlib.sha256(key.encode()).hexdigest(), tier, public, at.timestamp()


class News:
    def __init__(self, schedule, storage):
        self.schedule = schedule
        self.storage = storage

    async def offer(self, args, now):
        try:
            key, tier, public, stamp = parse_offer(args)
        except (AttributeError, KeyError, TypeError, ValueError, OverflowError, OSError):
            return "unavailable"
        items = self.schedule.state.news
        item = items.get(key)
        prior = item or await self.storage.get("news." + key) or {}
        if prior.get("tier") == "breaking" or prior.get("tier") == tier:
            return "duplicate"
        expires = min(now.timestamp(), stamp) + WEEK
        if expires <= now.timestamp():
            return "unavailable"
        candidate = {
            "tier": tier,
            "expires": expires,
            "due": 0,
            "mentioned": prior.get("mentioned", False),
        }
        if tier == "brief":
            candidate.update(event_at=stamp, source_count=len(public["sources"]))
            self.schedule.brief_time(now)
        snapshot = self.schedule.dump()
        snapshot["news"][key] = candidate
        # SDK 单值上限为 1 MiB。给同批开口的键列表和收据元数据预留位置。
        size = len(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")).encode())
        if size + 67 * len(snapshot["news"]) + 4096 > STORAGE_LIMIT:
            return "unavailable"
        # 先写材料，再把键加入调度。崩溃时重交 offer 仍能完成入队。
        # 分档独立保存，未持久化的升级不能替换原简报材料。
        try:
            await self.storage.set("news." + key + "." + tier, public)
        except ValueError:
            return "unavailable"
        await self.storage.set("schedule", snapshot)
        items[key] = candidate
        if item and item["tier"] == "brief":
            await self.storage.delete("news." + key + ".brief")
        return "accepted"

    async def restore(self):
        active = self.schedule.state.news
        for key, item in list(active.items()):
            if item["tier"] == "brief" and "event_at" not in item:
                public = await self.storage.get("news." + key + ".brief")
                if public is None and await self.storage.get("news." + key):
                    # 上次已清理材料并留下收据，调度状态还没来得及保存。
                    del active[key]
                    continue
                item.update(
                    event_at=datetime.fromisoformat(public["time"]).timestamp(),
                    source_count=len(public["sources"]),
                )
        # 清理两次写入之间退出所留下的材料，保留去重收据。
        for stored in await self.storage.keys("news."):
            parts = stored.split(".")
            if len(parts) == 3 and (parts[1] not in active or active[parts[1]]["tier"] != parts[2]):
                await self.storage.delete(stored)

    async def retire(self, now):
        state = self.schedule.state
        in_flight = {
            key for opening in state.pending.values() for key in opening.get("news_keys", [])
        }
        if state.submitting:
            in_flight.update(state.submitting.get("news_keys", []))
        changed = False
        for key, item in list(state.news.items()):
            stale_brief = item["tier"] == "brief" and now.timestamp() - item["event_at"] > DAY
            if key in in_flight or (
                not item.get("closed") and not stale_brief and item["expires"] > now.timestamp()
            ):
                continue
            await self.storage.set(
                "news." + key, {"tier": item["tier"], "mentioned": item["mentioned"]}
            )
            for tier in ("brief", "breaking"):
                await self.storage.delete("news." + key + "." + tier)
            del state.news[key]
            changed = True
        return changed

    async def material(self, opening):
        tier = "brief" if opening.reason == "news_brief" else "breaking"
        rows = []
        for key in opening.news_keys:
            public = await self.storage.get("news." + key + "." + tier)
            # 提醒是按事件给的，同批其他事件仍然可以正常说。
            if self.schedule.state.news[key]["mentioned"]:
                public["possibly_mentioned"] = True
            rows.append(public)
        return {"events": rows}
