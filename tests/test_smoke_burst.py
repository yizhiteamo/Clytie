"""Burst and reference cases exercised through the real tester-side driver."""

import pytest
import yaml

from .test_smoke_run import Scenario, save


def case(label, *, reply=True, **fields):
    return dict(
        id=label, expect=dict(reply=reply, image=False, reactions="none"), timeout_s=15, **fields
    )


class Burst(Scenario):
    def __init__(self, home, cases, *, limit=6):
        super().__init__(home, "burst")
        smoke = home / "smoke"
        cfg = yaml.safe_load((smoke / "smoke.yaml").read_bytes())
        cfg["limits"]["max_cases"] = limit
        save(smoke / "smoke.yaml", cfg)
        save(smoke / "cases.yaml", {"cases": cases})
        self.checks = 0
        self.fail_member_check = self.change_target = self.stray_before_send = 0
        self.deliveries = {}
        self.reactions = {}

    def driver(self, home, plugin, clock):
        driver = super().driver(home, plugin, clock)

        def observe(mid):
            driver.observe(
                dict(
                    post_type="message",
                    message_type="group",
                    user_id=self.bot,
                    group_id=self.group,
                    message_id=mid,
                    message=[{"type": "text", "data": {"text": "private response"}}],
                )
            )

        async def rpc(action, params):
            if action == "get_group_member_list":
                self.checks += 1
                if self.checks == self.stray_before_send:
                    observe("synthetic-out-unrelated")
                members = [self.bot, self.login]
                if self.checks == self.fail_member_check:
                    members.append("outsider")
                return [{"user_id": member} for member in members]
            if action == "get_login_info":
                if self.checks == self.change_target:
                    cfg = yaml.safe_load((home / "clytie.local.yaml").read_bytes())
                    cfg["conversations"]["test_group"]["platform_id"] = "changed-target"
                    save(home / "clytie.local.yaml", cfg)
                return {"user_id": self.login}
            assert action == "send_group_msg" and params["group_id"] == self.group
            self.sent.append((clock.monotonic(), params["message"]))
            number = len(self.sent)
            for target, emoji, added in self.reactions.get(number, ()):
                driver.observe(
                    dict(
                        post_type="notice",
                        notice_type="group_msg_emoji_like",
                        group_id=self.group,
                        user_id=self.bot,
                        message_id=f"synthetic-in-{target}",
                        is_add=added,
                        likes=[{"emoji_id": emoji}],
                    )
                )
            for mid in self.deliveries.get(number, []):
                observe(mid)
            if number in self.deliveries:
                self.rows = [
                    dict(
                        sender_id="tester",
                        created_at=clock.now().isoformat(),
                        exec_status="ok",
                        delivery_status="sent",
                        reaction_outcome=None,
                        tokens={},
                        deliveries=[
                            dict(state=None, status="sent", status_reaction=False)
                            for _ in self.deliveries[number]
                        ],
                    )
                ]
            return {"message_id": f"synthetic-in-{number}"}

        driver.rpc = rpc
        return driver


def three_texts():
    return case("burst", texts=["first", "second", "third"], mention=False)


async def test_message_limit_precedes_consent_and_sends_nothing(tmp_path):
    run = Burst(tmp_path, [three_texts()], limit=2)
    assert (await run.execute())["failure"] == "case_limit"
    assert run.prompts == run.sent == []


@pytest.mark.parametrize(
    "fields",
    [
        {"text": "also text"},
        {"image": "picture.png"},
        {"reply_to_last": True},
        {"texts": []},
        {"texts": ["single"]},
        {"texts": ["ok", ""]},
        {"texts": "bad"},
    ],
)
async def test_invalid_burst_rejected_before_consent(tmp_path, fields):
    run = Burst(tmp_path, [{**three_texts(), **fields}])
    assert (await run.execute())["failure"] == "invalid_case"
    assert run.prompts == run.sent == []


async def test_burst_mentions_only_on_first_send(tmp_path):
    run = Burst(tmp_path, [case("burst", texts=["first", "second", "third"])])
    run.deliveries[3] = ["synthetic-out-reply"]
    assert (await run.execute())["result"] == "passed"
    assert [[p["type"] for p in parts] for _, parts in run.sent] == [
        ["at", "text"],
        ["text"],
        ["text"],
    ]


async def test_confirmation_counts_cases_and_messages_and_sends_in_order(tmp_path):
    run = Burst(tmp_path, [three_texts()], limit=3)
    run.deliveries[3] = ["synthetic-out-reply"]
    result = await run.execute()
    assert result["result"] == "passed", result
    assert "1 条用例" in run.prompts[0] and "3 条消息" in run.prompts[0]
    assert run.checks == 3
    times, messages = zip(*run.sent)
    assert [times[1] - times[0], times[2] - times[1]] == pytest.approx([1, 1])
    assert messages == tuple(
        [{"type": "text", "data": {"text": s}}] for s in ("first", "second", "third")
    )
    assert len(result["cases"]) == len(result["cases"][0]["replies"]) == 1


@pytest.mark.parametrize("failure", ["member_mismatch", "target_changed"])
async def test_burst_rechecks_members_and_target_before_each_send(tmp_path, failure):
    run = Burst(tmp_path, [three_texts()])
    if failure == "member_mismatch":
        run.fail_member_check = 2
    else:
        run.change_target = 2
    result = await run.execute()
    assert result["result"] == "failed" and result["cases"][0]["failure"] == failure
    assert len(run.sent) == 1 and run.checks == 2


async def test_reply_quotes_only_last_observation_of_previous_case(tmp_path):
    run = Burst(
        tmp_path,
        [case("one", text="hello"), case("two", text="again", reply_to_last=True, mention=False)],
    )
    run.deliveries = {1: ["synthetic-out-first", "synthetic-out-last"], 2: ["synthetic-out-next"]}
    run.stray_before_send = 2
    assert (await run.execute())["result"] == "passed"
    assert run.sent[1][1] == [
        {"type": "reply", "data": {"id": "synthetic-out-last"}},
        {"type": "text", "data": {"text": "again"}},
    ]


async def test_reply_after_silent_case_never_reuses_an_older_reply(tmp_path):
    run = Burst(
        tmp_path,
        [
            case("one", text="hello"),
            case("silent", text="quiet", reply=False),
            case("three", text="again", reply_to_last=True),
        ],
    )
    run.deliveries = {1: ["synthetic-out-first"], 2: []}
    result = await run.execute()
    assert result["result"] != "passed" and len(run.sent) == 2


async def test_reply_seen_between_burst_sends_remains_in_verdict(tmp_path):
    run = Burst(tmp_path, [three_texts()])
    run.stray_before_send = 2
    run.deliveries = {3: ["synthetic-out-last"]}
    result = await run.execute()
    assert result["cases"][0]["failure"] == "unattributed_reply"


async def test_burst_cannot_combine_status_reactions_from_different_messages(tmp_path):
    burst = three_texts()
    burst["expect"]["reactions"] = ["accepted", "done"]
    run = Burst(tmp_path, [burst])
    run.reactions[3] = [(1, "a", True), (2, "a", False), (2, "c", True)]
    run.deliveries[3] = ["synthetic-out-last"]
    result = await run.execute()
    assert result["cases"][0]["failure"] == "ambiguous_reaction_target"
