"""User-confirmed sequential smoke run."""

import asyncio
import json
import math
from contextlib import suppress
from datetime import datetime

from clytie.api.client import Client
from clytie.clock import SystemClock
from clytie.config import repository_root
from clytie.plugin_host import PluginHost
from clytie.plugin_host.support import timed
from clytie.sdk.immutable import freeze, thaw
from clytie.sdk.smoke import SmokeDriverError, SmokeReaction, SmokeReply

from .config import ConfiguredSecrets, SmokeError, case_image, prepare, require, target_config
from .judge import verdict


async def get(client, path, query=None):
    response = await client.request("GET", "/api/v1/" + path, query=query)
    require(response.status == 200 and response.json.get("ok"), "api_failed")
    return thaw(response.json["result"])


def load_driver(home, plugin, clock):
    host = PluginHost(repository_root(), home, clock=clock)
    manifest = host.manifests.get(plugin)
    require(manifest is not None, "driver_unavailable")
    declaration = manifest.data["contributes"].get("smoke_driver")
    require(declaration, "driver_unavailable")
    cls, _ = host._load_class(manifest)
    return getattr(cls(), declaration["factory"])(clock)


async def exercise(case, driver, client, cid, tester, clock, interval, timeout_result, image=None):
    since = clock.now()
    start = clock.monotonic()
    mid = (
        await driver.send({k: v for k, v in case.items() if k != "image"}, image=image)
        if image is not None
        else await driver.send(case)
    )
    require(isinstance(mid, str) and mid, "send_unknown")
    timeout_result["failure"] = "timeout_record"

    replies, reactions = [], []

    async def listen():
        async for event in driver.events():
            require(
                isinstance(event, (SmokeReply, SmokeReaction))
                and type(event.at) in {int, float}
                and math.isfinite(event.at),
                "invalid_observation",
            )
            if isinstance(event, SmokeReply):
                require(
                    type(event.chars) is int and event.chars >= 0 and type(event.image) is bool,
                    "invalid_observation",
                )
                replies.append(dict(chars=event.chars, image=event.image, at=event.at))
            elif event.message_id == mid:
                require(
                    event.state in {"accepted", "running", "done", "failed"}
                    and type(event.added) is bool,
                    "invalid_observation",
                )
                reactions.append((event.state, event.added, event.at))
            require(len(replies) + len(reactions) <= 2048, "event_overflow")
        raise SmokeError("driver_closed")

    reader = asyncio.create_task(listen())
    ledger_terminal_at = None
    try:
        while True:
            rows = await get(
                client, f"companions/{cid}/smoke/records", {"since": since.isoformat()}
            )
            if reader.done():
                reader.result()
            matches = [
                r
                for r in rows
                if r["sender_id"] == tester and datetime.fromisoformat(r["created_at"]) >= since
            ]
            require(len(matches) <= 1, "ambiguous_record")
            require(
                not any(
                    delivery["status"] == "sent"
                    for row in rows
                    if not matches or row is not matches[0]
                    for delivery in row["deliveries"]
                ),
                "interference",
            )
            expected = case["expect"]["reactions"]
            if matches and expected != "none" and ledger_terminal_at is None:
                if any(
                    d["state"] == expected[-1] and d["status"] == "sent"
                    for d in matches[0]["deliveries"]
                ):
                    ledger_terminal_at = clock.monotonic()
            answer = verdict(
                case,
                matches[0] if matches else None,
                replies,
                reactions,
                clock.monotonic(),
                interval,
                start,
                ledger_terminal_at,
            )
            if isinstance(answer, dict):
                return answer
            timeout_result["failure"] = answer
            await clock.sleep(0.2)
    finally:
        reader.cancel()
        with suppress(asyncio.CancelledError):
            await reader


async def run(
    home, *, ask=input, emit=print, driver_loader=load_driver, client_type=Client, clock=None
):
    clock = clock or SystemClock()
    started = clock.now()
    report = {"started_at": started.isoformat(), "result": "stopped", "cases": []}
    driver = None
    try:
        cfg, cases, target = prepare(home)
        cid = cfg["companion"]
        report["companion"] = cid
        images = sum(c["expect"]["image"] for c in cases)
        require(
            ask(f"{cid}，{len(cases)} 条用例，预计 {images} 张图片。输入 y 运行：").strip() == "y",
            "not_confirmed",
        )
        client = client_type(home)
        row = (await get(client, "status"))["companions"].get(cid, {})
        require(
            row.get("state") == "running"
            and row.get("channels", {}).get(target.channel) == "online"
            and row.get("admission", {}).get("mode")
            in {"registered", "test_only", "owner_present"},
            "companion_unavailable",
        )
        driver = driver_loader(home, target.channel, clock)
        config = freeze(cfg["driver"]["config"])
        await driver.connect(config, ConfiguredSecrets(home, config))
        visual = []
        for case in cases:
            require(target_config(home, cid, cfg["tester"]) == target, "target_changed")
            count, account = await driver.authorize(target)
            require(
                type(count) is int
                and 0 < count <= len(target.allowed_accounts)
                and type(account) is int
                and 1 <= account <= len(target.tester_accounts),
                "invalid_authorization",
            )
            report.update(member_count=count, tester_account_index=account)
            require(target_config(home, cid, cfg["tester"]) == target, "target_changed")
            timeout_result = {"result": "failed", "failure": "timeout_send"}
            try:
                item = await timed(
                    exercise(
                        case,
                        driver,
                        client,
                        cid,
                        cfg["tester"],
                        clock,
                        target.min_interval_seconds,
                        timeout_result,
                        case_image(home, case),
                    ),
                    clock,
                    case["timeout_s"],
                )
            except (asyncio.TimeoutError, TimeoutError):
                item = timeout_result
            except (SmokeError, SmokeDriverError) as error:
                item = {"result": "failed", "failure": str(error)}
            except Exception:
                item = {"result": "failed", "failure": "observation_failed"}
            item["id"] = case["id"]
            if "image" in case and item["result"] != "failed":
                item["image_visual_check"] = True
                item["result"] = "needs_visual_check"
            report["cases"].append(item)
            emit(
                f"{item['id']} {item['result']} {item.get('reply_s', '-')}s {item.get('reactions', [])}"
                + (f" {item['failure']}" if item.get("failure") else "")
            )
            if item["result"] == "failed":
                report["result"] = "failed"
                break
            if item["result"] == "needs_visual_check":
                visual.append(item)
        else:
            confirmed = (
                not visual
                or ask(
                    (
                        "请目视确认带图用例回复与图片相符，并核对要求的最终表情："
                        if any(i.get("image_visual_check") for i in visual)
                        else "请目视确认最终只剩一个表情："
                    )
                    + ", ".join(i["id"] for i in visual)
                    + "；输入 y："
                ).strip()
                == "y"
            )
            for item in visual:
                item["visual_confirmed"] = confirmed
            report["result"] = "passed" if confirmed else "failed"
    except (SmokeError, SmokeDriverError) as error:
        report["failure"] = str(error)
    except Exception:
        report["failure"] = "smoke_unavailable"
    finally:
        if driver:
            try:
                await driver.close()
            except Exception:
                report["result"] = "failed"
                report.setdefault("failure", "driver_close_failed")
    directory = home / "smoke/reports"
    require(directory.resolve() == home.resolve() / "smoke/reports", "invalid_report_path")
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / (started.strftime("%Y%m%dT%H%M%S%f") + ".json")).open(
        "x", encoding="utf-8"
    ) as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    tokens = sum(sum(i.get("tokens", {}).values()) for i in report["cases"])
    images = sum(r["image"] for i in report["cases"] for r in i.get("replies", []))
    emit(f"{report['result']} tokens={tokens} images={images}")
    return report


def main(home):
    try:
        return int(asyncio.run(run(home))["result"] != "passed")
    except (Exception, KeyboardInterrupt):
        print("smoke_stopped")
        return 1
