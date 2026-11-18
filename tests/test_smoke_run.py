"""Offline B5: synthetic OneBot frames, local API responses and an injected clock."""

import asyncio
import json

import pytest
import yaml

from clytie.sdk.testing import MockClock
from clytie.sdk.types import ApiResponse
from clytie.smoke.driver import Driver
from clytie.smoke.runner import run


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


class Scenario:
    def __init__(self, home, scenario):
        self.home, self.scenario = home, scenario
        self.clock = MockClock()
        self.sent, self.rows, self.prompts, self.output = [], [], [], []
        self.closed = False
        self.login = "fictional-secondary"
        self.group = "fictional-target"
        self.bot = "fictional-companion-account"
        self.text = "synthetic private case text"
        smoke = home / "smoke"
        save(
            smoke / "smoke.yaml",
            dict(
                tester="tester",
                companion="companion_a",
                ws_url="ws://localhost",
                token_file="secrets/smoke.key",
                limits={
                    "max_cases": 1 if scenario == "case_limit" else 2,
                    "max_images": 0 if scenario == "image_limit" else 2,
                },
            ),
        )
        expect = dict(reply=True, image=True, reactions=["accepted", "optional:running", "done"])
        save(
            smoke / "cases.yaml",
            {
                "cases": [
                    dict(id=i, text=self.text, expect=expect, timeout_s=10)
                    for i in ("draw", "edit")
                ]
            },
        )
        save(
            home / "clytie.local.yaml",
            dict(
                companions=["companion_a"],
                test_conversation="test_group",
                accounts={"binding": {"channel": "qq-onebot", "account": self.bot}},
                people={
                    "tester": {
                        "role": "member",
                        "accounts": [
                            {"channel": "qq-onebot", "account": a}
                            for a in ("fictional-primary", self.login)
                        ],
                    }
                },
                conversations={
                    "test_group": dict(
                        kind="group",
                        channel="qq-onebot",
                        platform_id=self.group,
                        members_expected=["binding", "tester"],
                    )
                },
            ),
        )
        save(
            home / "companions/companion_a/companion.yaml",
            dict(
                channels=[
                    dict(
                        plugin="qq-onebot",
                        account="binding",
                        config={"emoji_ids": {"👌": "a", "⏳": "b", "✅": "c"}},
                    )
                ]
            ),
        )
        token_path = home / "secrets/smoke.key"
        token_path.parent.mkdir()
        token_path.write_text("synthetic-marker", encoding="utf-8")

    def driver(self, target, clock):
        scenario = self

        class FakeDriver(Driver):
            async def connect(self, url, token):
                assert token == "synthetic-marker"

            async def close(self):
                scenario.closed = True

            async def read(self):
                await asyncio.Future()

            async def rpc(self, action, params):
                name = scenario.scenario
                if action == "get_login_info":
                    return {"user_id": "outsider" if name == "account_mismatch" else scenario.login}
                if action == "get_group_member_list":
                    members = [scenario.login, scenario.bot]
                    if name == "member_mismatch":
                        members.append("outsider")
                    return [{"user_id": i} for i in members]
                assert action == "send_group_msg" and params["group_id"] == scenario.group
                assert params["message"] == [
                    {"type": "at", "data": {"qq": scenario.bot}},
                    {"type": "text", "data": {"text": scenario.text}},
                ]
                scenario.sent.append(action)
                n = len(scenario.sent)
                mid = f"synthetic-in-{n}"
                common = dict(group_id=scenario.group, user_id=scenario.bot)
                deliveries = []
                for kind in ("text", "image"):
                    out = f"synthetic-out-{n}-{kind}"
                    deliveries.append(dict(state=None, status="sent", status_reaction=False))
                    if kind == "image" and name == "missing_delivery":
                        continue
                    self.observe(
                        dict(
                            common,
                            post_type="message",
                            message_type="group",
                            message_id=out,
                            message=[{"type": "text", "data": {"text": "private response"}}]
                            if kind == "text" or name == "missing_image"
                            else [{"type": "image", "data": {}}],
                        )
                    )
                if name == "extra":
                    self.observe(
                        dict(
                            common,
                            post_type="message",
                            message_type="group",
                            message_id="stray",
                            message=[{"type": "text", "data": {"text": "stray"}}],
                        )
                    )
                events = [("a", True), ("a", False), ("c", True)]
                if name in {"visual_yes", "visual_no"}:
                    events.pop(1)
                elif name == "wrong_order":
                    events = [("b", True), ("b", False), ("c", True)]
                elif name == "two_final":
                    events.append(("a", True))
                elif name in {"no_notices", "missing_terminal"}:
                    events = []
                self.observe(
                    dict(
                        common,
                        post_type="notice",
                        notice_type="group_msg_emoji_like",
                        message_id="other-tester-message",
                        is_add=True,
                        likes=[{"emoji_id": "b"}],
                    )
                )
                for emoji, add in events:
                    clock.advance(0.01)
                    self.observe(
                        dict(
                            common,
                            post_type="notice",
                            notice_type="group_msg_emoji_like",
                            message_id=mid,
                            is_add=add,
                            likes=[{"emoji_id": emoji}],
                        )
                    )
                if name == "receipt_failed":
                    deliveries[0]["status"] = "failed"
                deliveries += [
                    dict(state=s, status="sent", status_reaction=True)
                    for s in (("accepted",) if name == "missing_terminal" else ("accepted", "done"))
                ]
                scenario.rows = [
                    dict(
                        sender_id="tester",
                        created_at=clock.now().isoformat(),
                        exec_status="ok",
                        delivery_status="sent",
                        reaction_outcome=None,
                        tokens={"input": 5, "output": 2},
                        deliveries=deliveries,
                    )
                ]
                if name == "no_record":
                    scenario.rows.clear()
                elif name == "ambiguous":
                    scenario.rows.append(dict(scenario.rows[0], deliveries=[]))
                elif name == "previous_case_sent":
                    scenario.rows.append(
                        dict(scenario.rows[0], created_at="1999-12-31T00:00:00+00:00")
                    )
                elif name in {"interference", "foreign_pending"}:
                    scenario.rows.append(
                        dict(
                            sender_id="other",
                            deliveries=[
                                dict(
                                    state="accepted",
                                    status_reaction=True,
                                    status="sent" if name == "interference" else "pending",
                                )
                            ],
                        )
                    )
                return {"message_id": mid}

        return FakeDriver(target, clock)

    async def request(self, method, path, query):
        assert method == "GET"
        if path.endswith("/status"):
            value = {
                "companions": {
                    "companion_a": {
                        "state": "stopped" if self.scenario == "offline" else "running",
                        "channels": {"qq-onebot": "online"},
                        "admission": {"mode": "test_only"},
                    }
                }
            }
        else:
            assert path == "/api/v1/companions/companion_a/smoke/records" and query["since"]
            value = self.rows
        return ApiResponse(status=200, json={"ok": True, "result": value})

    async def execute(self):
        def ask(prompt):
            self.prompts.append(prompt)
            return (
                "n"
                if self.scenario == "unconfirmed"
                or (self.scenario == "visual_no" and len(self.prompts) > 1)
                else "y"
            )

        task = asyncio.create_task(
            run(
                self.home,
                clock=self.clock,
                driver_type=self.driver,
                client_type=lambda home: self,
                ask=ask,
                emit=self.output.append,
            )
        )
        await self.clock.advance_and_drain(25)
        assert task.done()
        report = await task
        stored = next((self.home / "smoke/reports").glob("*.json")).read_text(encoding="utf-8")
        assert json.loads(stored) == report
        for private in (
            self.bot,
            self.group,
            self.login,
            self.text,
            "private response",
            "synthetic-in-",
            "synthetic-out-",
            "synthetic-marker",
        ):
            assert private not in stored + repr(self.output) + repr(self.prompts)
        return report


@pytest.mark.parametrize(
    "name, failure",
    [
        ("extra", "unattributed_reply"),
        ("missing_delivery", "timeout_replies"),
        ("no_record", "timeout_record"),
        ("missing_terminal", "timeout_reactions"),
        ("ambiguous", "ambiguous_record"),
        ("interference", "interference"),
        ("previous_case_sent", "interference"),
        ("missing_image", "reply_mismatch"),
        ("wrong_order", "reaction_order"),
        ("two_final", "final_reaction_failed"),
        ("receipt_failed", "receipt_failed"),
    ],
)
async def test_failure_stops_remaining_cases(tmp_path, name, failure):
    scenario = Scenario(tmp_path, name)
    report = await scenario.execute()
    assert scenario.closed and len(scenario.sent) == 1
    assert report["result"] == "failed" and report["cases"][0]["failure"] == failure


@pytest.mark.parametrize(
    "name", ["passed", "foreign_pending", "visual_yes", "visual_no", "no_notices"]
)
async def test_split_reply_and_final_emoji(tmp_path, name):
    scenario = Scenario(tmp_path, name)
    report = await scenario.execute()
    assert scenario.closed and len(scenario.sent) == 2
    assert report["result"] == ("failed" if name == "visual_no" else "passed")
    for item in report["cases"]:
        assert [r["image"] for r in item["replies"]] == [False, True]
        if name in {"passed", "foreign_pending"}:
            assert item["final_status"] == "verified" and len(scenario.prompts) == 1
        else:
            assert item["final_status"] == "unverified" and item["result"] == "needs_visual_check"
            assert item["visual_confirmed"] == (name != "visual_no") and len(scenario.prompts) == 2
            if name == "no_notices":
                assert item["reaction_source"] == "ledger"


@pytest.mark.parametrize(
    "name",
    ["case_limit", "image_limit", "unconfirmed", "offline", "member_mismatch", "account_mismatch"],
)
async def test_safety_gates_never_send(tmp_path, name):
    scenario = Scenario(tmp_path, name)
    assert (await scenario.execute())["result"] == "stopped"
    assert not scenario.sent


@pytest.mark.parametrize("primary", [True, False])
async def test_any_registered_tester_account(tmp_path, primary):
    scenario = Scenario(tmp_path, "passed")
    if primary:
        scenario.login = "fictional-primary"
    report = await scenario.execute()
    assert report["tester_account_index"] == (1 if primary else 2)
    assert report["result"] == "passed"
