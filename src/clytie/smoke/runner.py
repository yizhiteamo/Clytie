"""User-confirmed sequential smoke run."""

import asyncio
import json
from contextlib import suppress
from datetime import datetime

from clytie.api.client import Client
from clytie.clock import SystemClock
from clytie.plugin_host.support import timed
from clytie.sdk.immutable import thaw

from .config import SmokeError, prepare, require, target_config
from .driver import Driver
from .judge import verdict


async def get(client, path, query=None):
    response = await client.request("GET", "/api/v1/" + path, query=query)
    require(response.status == 200 and response.json.get("ok"), "api_failed")
    return thaw(response.json["result"])


async def exercise(case, driver, client, cid, tester, clock, timeout_result):
    since = clock.now()
    start = clock.monotonic()
    mid = await driver.send(case)
    timeout_result["failure"] = "timeout_record"

    async def listen():
        while True:
            await driver.read()

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
            replies = [v for kind, _, v in driver.events if kind == "reply"]
            reactions = [v for kind, n, v in driver.events if kind == "reaction" and n == mid]
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
                driver.target["interval"],
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


async def run(home, *, ask=input, emit=print, driver_type=Driver, client_type=Client, clock=None):
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
            and row.get("channels", {}).get("qq-onebot") == "online"
            and row.get("admission", {}).get("mode")
            in {"registered", "test_only", "owner_present"},
            "companion_unavailable",
        )
        driver = driver_type(target, clock)
        token = (home / cfg["token_file"]).read_text(encoding="utf-8").strip()
        require(token, "missing_ws_token")
        await driver.connect(cfg["ws_url"], token)
        visual = []
        for case in cases:
            require(target_config(home, cid, cfg["tester"]) == target, "target_changed")
            count, account = await driver.authorize()
            report.update(member_count=count, tester_account_index=account)
            require(target_config(home, cid, cfg["tester"]) == target, "target_changed")
            timeout_result = {"result": "failed", "failure": "timeout_send"}
            try:
                item = await timed(
                    exercise(case, driver, client, cid, cfg["tester"], clock, timeout_result),
                    clock,
                    case["timeout_s"],
                )
            except (asyncio.TimeoutError, TimeoutError):
                item = timeout_result
            except SmokeError as error:
                item = {"result": "failed", "failure": str(error)}
            except Exception:
                item = {"result": "failed", "failure": "observation_failed"}
            item["id"] = case["id"]
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
                    "请目视确认最终只剩一个表情："
                    + ", ".join(i["id"] for i in visual)
                    + "；输入 y："
                ).strip()
                == "y"
            )
            for item in visual:
                item["visual_confirmed"] = confirmed
            report["result"] = "passed" if confirmed else "failed"
    except SmokeError as error:
        report["failure"] = str(error)
    except Exception:
        report["failure"] = "smoke_unavailable"
    finally:
        if driver:
            await driver.close()
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
