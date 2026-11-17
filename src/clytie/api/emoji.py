"""Interactive capture presentation; all state and writes go through local API."""

import asyncio
import re

from clytie.clock import SystemClock
from clytie.sdk.immutable import thaw
from clytie.sdk.types import ApiResponse


async def capture(cli, cid, seconds=180, *, clock=None):
    clock = clock or SystemClock()
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", cid):
        raise ValueError("invalid_companion")
    prefix = f"/companions/{cid}/plugins/qq-onebot/emoji/"
    response = await cli.request("POST", prefix + "start", {"seconds": seconds})
    if not response.json.get("ok"):
        return response
    session = response.json["result"]["session"]
    try:
        print(
            "请在一条没有其他表情的消息上添加。请依次添加接单、执行中、完成、失败的四个表情；仅接受 owner。"
        )
        while True:
            response = await cli.request("GET", prefix + "status", query={"session": session})
            if not response.json.get("ok"):
                return response
            value = response.json["result"]
            if value["state"] != "collecting":
                break
            await clock.sleep(0.25)
        if value["state"] != "complete":
            print("未完成采集：" + value["state"] + "；不会写入配置。")
            return ApiResponse(
                status=409,
                json={"ok": False, "error": {"code": "capture_incomplete"}, "result": thaw(value)},
            )
        for label, number in zip(("接单", "执行中", "完成", "失败"), value["ids"]):
            print(label + ": " + number)
        answer = await asyncio.to_thread(cli.input, "确认保存以上编号？[y/N] ")
        if answer.strip().lower() != "y":
            print("未确认，不写入配置。")
            return response
        return await cli.request("POST", f"/companions/{cid}/emoji/apply", {"session": session})
    finally:
        await cli.request("POST", prefix + "cancel", {"session": session})
