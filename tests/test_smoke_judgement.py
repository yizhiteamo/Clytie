from clytie.smoke.judge import verdict


def test_ledger_fallback_requires_visual_check_and_waits_for_late_removal():
    case = {"expect": {"reply": True, "image": False, "reactions": ["accepted", "done"]}}
    record = {
        "deliveries": [
            {"state": s, "status": "sent", "status_reaction": s is not None}
            for s in (None, "accepted", "done")
        ],
        "exec_status": "ok",
        "delivery_status": "sent",
        "reaction_outcome": None,
        "tokens": {},
    }
    replies = [{"at": 1, "image": False, "chars": 3}]

    def check(events, now):
        return verdict(case, record, replies, events, now, 2, 0, ledger_terminal_at=8)

    assert check([], 12.99) == "timeout_reactions"
    fallback = check([], 13)
    assert fallback["reaction_source"] == "ledger" and fallback["result"] == "needs_visual_check"
    events = [("accepted", True, 1), ("done", True, 8)]
    assert check(events, 12.99) == "timeout_reactions"
    assert check(events + [("accepted", False, 12)], 13)["final_status"] == "verified"
    assert check(events + [("done", False, 12)], 13)["final_status"] == "failed"


def test_yes_option_does_not_supply_consent(tmp_path, monkeypatch):
    from clytie.cli import main
    from clytie.smoke import runner

    from .test_smoke_run import Scenario

    Scenario(tmp_path, "passed")
    prompts = []
    execute = runner.run

    async def refuse(home):
        return await execute(home, ask=lambda question: prompts.append(question) or "n")

    monkeypatch.setattr(runner, "run", refuse)
    assert main(["smoke", "run", "--yes", "--home", str(tmp_path)]) == 1
    assert len(prompts) == 1
