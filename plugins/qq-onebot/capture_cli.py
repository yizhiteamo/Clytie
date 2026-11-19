"""Interactive presentation of a capture session over the local API."""

import asyncio


async def capture(args, api):
    prefix = f"/api/v1/companions/{args['companion']}/plugins/qq-onebot/"
    response = await api.request(
        "POST", prefix + "emoji/start", json={"seconds": args.get("seconds", 180)}
    )
    if not response.json["ok"]:
        print("无法开始采集。", response.json["error"]["code"])
        return 1
    session = response.json["result"]["session"]
    try:
        print(
            "请在一条没有其他表情的消息上添加。请依次添加接单、执行中、完成、失败的四个表情；仅接受 owner。"
        )
        while True:
            response = await api.request(
                "GET", prefix + "emoji/status", query={"session": session, "wait": "1"}
            )
            if not response.json["ok"]:
                print("无法读取采集状态。", response.json["error"]["code"])
                return 1
            value = response.json["result"]
            if value["state"] != "collecting":
                break
        if value["state"] != "complete":
            print("未完成采集：" + value["state"])
            return 1
        for label, number in zip(("接单", "执行中", "完成", "失败"), value["ids"]):
            print(label + "：" + number)
        answer = await asyncio.to_thread(input, "确认保存以上编号？[y/N] ")
        if answer.strip().lower() != "y":
            print("已取消保存。")
            return 0
        response = await api.request(
            "POST", prefix + "config/apply", json={"session": session, "confirm": True}
        )
        if not response.json["ok"]:
            print("保存失败。", response.json["error"]["code"])
            return 1
        print("编号已保存，重启伴侣后生效。")
        return 0
    finally:
        await api.request("POST", prefix + "emoji/cancel", json={"session": session})
