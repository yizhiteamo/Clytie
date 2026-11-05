"""Review rounds fail closed and never publish unscanned or stale reports."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest


def load_tool(name):
    path = Path(__file__).resolve().parents[1] / "scripts" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


protocol_module = load_tool("review_protocol")
codex_review = load_tool("codex_review")
ReviewIncomplete = protocol_module.ReviewIncomplete
ReviewProtocol = protocol_module.ReviewProtocol

HEAD = "a" * 40
BASE = "b" * 40


def metadata():
    return {
        "repository": "fictional/repository",
        "pr": 1,
        "base": BASE,
        "head": HEAD,
        "ci": {"head": HEAD, "passed": True},
        "scans": {"generic": True, "private": True},
        "approved_specs": ["docs/spec.md"],
        "checklist": ["offline"],
        "rules": "c" * 64,
        "cli_version": "0.166.0-alpha.6",
        "model": "gpt-6-astra",
        "effort": "high",
    }


def report():
    return {
        "head": HEAD,
        "conclusion": "通过",
        "findings": [],
        "evidence": ["fictional verification"],
        "uncovered": [],
    }


def test_review_trigger_and_head_binding(tmp_path):
    protocol = ReviewProtocol(tmp_path, 1)
    bad = metadata()
    bad["ci"]["head"] = BASE
    with pytest.raises(ReviewIncomplete):
        protocol.reserve(bad)
    assert protocol.rounds() == []
    bad = metadata()
    bad["scans"]["private"] = False
    with pytest.raises(ReviewIncomplete):
        protocol.reserve(bad)
    assert protocol.rounds() == []


def test_report_missing_invalid_and_wrong_head_never_pass(tmp_path):
    protocol = ReviewProtocol(tmp_path, 1)
    number = protocol.reserve(metadata())
    for raw in ("", "{}", json.dumps({**report(), "head": BASE})):
        with pytest.raises(ReviewIncomplete):
            protocol.validate(number, raw)
    assert protocol.rounds()[0]["status"] == "review_incomplete"


def test_scanner_intercepts_and_head_rechecked_without_duplicate_publish(tmp_path):
    protocol = ReviewProtocol(tmp_path, 1)
    number = protocol.reserve(metadata())
    raw = json.dumps(report(), ensure_ascii=False)
    protocol.validate(number, raw)
    published = []
    with pytest.raises(ReviewIncomplete):
        protocol.publish(number, head=lambda: HEAD, scan=lambda text: False, send=published.append)
    assert not published
    with pytest.raises(ReviewIncomplete):
        protocol.publish(number, head=lambda: BASE, scan=lambda text: True, send=published.append)
    assert not published
    protocol.publish(
        number,
        head=lambda: HEAD,
        scan=lambda text: True,
        send=lambda text: published.append(text) or "fictional-comment",
    )
    protocol.publish(number, head=lambda: HEAD, scan=lambda text: True, send=published.append)
    assert published == [raw]


def test_round_limit_and_same_head_not_restarted(tmp_path):
    protocol = ReviewProtocol(tmp_path, 1)
    protocol.reserve(metadata())
    with pytest.raises(ReviewIncomplete):
        protocol.reserve(metadata())
    for letter in ("d", "e"):
        changed = metadata()
        changed["head"] = changed["ci"]["head"] = letter * 40
        protocol.reserve(changed)
    changed = metadata()
    changed["head"] = changed["ci"]["head"] = "f" * 40
    with pytest.raises(ReviewIncomplete):
        ReviewProtocol(tmp_path, 1).reserve(changed)
    assert len(protocol.rounds()) == 3


def test_codex_command_pins_model_mxc_and_cleans_tool_environment(tmp_path):
    client_environment, command = codex_review.client_environment, codex_review.command

    args = command(
        "codex.exe",
        tmp_path / "snapshot with space",
        tmp_path / "report.txt",
        tmp_path / "schema.json",
    )
    assert args[args.index("-m") + 1] == "gpt-6-astra"
    assert 'model_reasoning_effort="high"' in args
    assert 'windows.sandbox="mxc"' in args
    assert "--ignore-user-config" in args and "--ignore-rules" in args
    assert "--no-daemon" in args and "--ephemeral" in args
    assert "--strict-config" in args
    assert 'shell_environment_policy.inherit="none"' in args
    assert "permissions.clytie-review.network.enabled=false" in args
    assert not any(
        flag in args
        for flag in (
            "resume",
            "fork",
            "--dangerously-bypass-approvals-and-sandbox",
            "--approve-for-me",
        )
    )
    assert client_environment(
        {
            "SystemDrive": "C:",
            "CLYTIE_HOME": "fictional-private",
            "GH_TOKEN": "fictional-value",
            "OPENAI_API_KEY": "fictional-value",
        }
    ) == {"SystemDrive": "C:"}


def test_review_archive_rejects_links_and_traversal(tmp_path):
    import io
    import stat
    import zipfile

    extract_snapshot = codex_review.extract_snapshot

    for index, name in enumerate(("../escape.txt", "C:/escape.txt")):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr(name, "fictional")
        with pytest.raises(ReviewIncomplete):
            extract_snapshot(data.getvalue(), tmp_path / str(index))
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as archive:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "../outside")
    with pytest.raises(ReviewIncomplete):
        extract_snapshot(data.getvalue(), tmp_path / "link-snapshot")


def test_changed_report_and_uncertain_publication_cannot_publish_again(tmp_path):
    protocol = ReviewProtocol(tmp_path, 1)
    number = protocol.reserve(metadata())
    raw = json.dumps(report(), ensure_ascii=False)
    protocol.validate(number, raw)
    path = protocol.directory / "round-1/report.txt"
    path.write_text(raw + " ", encoding="utf-8")
    sent = []
    with pytest.raises(ReviewIncomplete, match="changed"):
        protocol.publish(number, head=lambda: HEAD, scan=lambda _: True, send=sent.append)
    assert not sent
    path.write_text(raw, encoding="utf-8")

    def uncertain(text):
        sent.append(text)
        raise OSError("fictional response lost")

    with pytest.raises(OSError):
        protocol.publish(number, head=lambda: HEAD, scan=lambda _: True, send=uncertain)
    with pytest.raises(ReviewIncomplete):
        protocol.publish(number, head=lambda: HEAD, scan=lambda _: True, send=sent.append)
    assert sent == [raw]


def test_malformed_ledger_does_not_reset_rounds(tmp_path):
    protocol = ReviewProtocol(tmp_path, 1)
    protocol.reserve(metadata())
    protocol.ledger.write_text("[{}]", encoding="utf-8")
    with pytest.raises(ReviewIncomplete):
        protocol.reserve(metadata())


def test_nonblocking_findings_remain_in_full_report(tmp_path):
    protocol = ReviewProtocol(tmp_path, 1)
    number = protocol.reserve(metadata())
    value = report()
    value["findings"] = [
        {
            "severity": "nonblocking",
            "problem": "fictional follow-up",
            "spec": "spec section",
            "location": "module:1",
            "evidence": "fictional test",
        }
    ]
    raw = json.dumps(value, ensure_ascii=False)
    assert protocol.validate(number, raw) == value
    assert (protocol.directory / "round-1/report.txt").read_text(encoding="utf-8") == raw


@pytest.mark.parametrize("outcome", ["success", "exit_failure", "missing_report"])
def test_fresh_review_fake_executor_and_incomplete_results(tmp_path, outcome):
    import hashlib
    from pathlib import Path
    from types import SimpleNamespace

    policy_hash, run_candidate = codex_review.policy_hash, codex_review.run_candidate

    candidate = tmp_path / "candidate"
    (candidate / "snapshot").mkdir(parents=True)
    (candidate / "metadata.json").write_text(json.dumps(metadata()), encoding="utf-8")
    for name in ("view.json", "diff.patch", "checks.txt", "ci.json", "scan-evidence.json"):
        (candidate / name).write_text("fictional evidence", encoding="utf-8")
    proof = tmp_path / "proof.json"
    proof.write_text("fictional preflight", encoding="utf-8")
    preflight = {
        "cli_version": metadata()["cli_version"],
        "policy_sha256": policy_hash(),
        "checks": dict.fromkeys(
            [
                "inside_read",
                "outside_read",
                "write_denied",
                "traversal",
                "links",
                "tool_environment",
                "network",
                "mcp_plugins",
                "effective_exec_policy",
            ],
            True,
        ),
        "evidence": [
            {"path": str(proof), "sha256": hashlib.sha256(proof.read_bytes()).hexdigest()}
        ],
    }
    protocol = ReviewProtocol(tmp_path / "reviews", 1)
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        assert kwargs["cwd"] == candidate / "snapshot"
        assert "CLYTIE_HOME" not in kwargs["env"]
        if outcome == "success":
            Path(args[args.index("-o") + 1]).write_text(json.dumps(report()), encoding="utf-8")
        return SimpleNamespace(returncode=1 if outcome == "exit_failure" else 0)

    with pytest.raises(ReviewIncomplete, match="preflight"):
        run_candidate(candidate, protocol, "fictional-codex", {}, executor=execute)
    assert not calls and protocol.rounds() == []
    if outcome == "success":
        assert (
            run_candidate(candidate, protocol, "fictional-codex", preflight, executor=execute)[1]
            == report()
        )
    else:
        with pytest.raises(ReviewIncomplete):
            run_candidate(candidate, protocol, "fictional-codex", preflight, executor=execute)
        assert protocol.rounds()[0]["status"] == "review_incomplete"
    assert len(calls) == 1
    with pytest.raises(ReviewIncomplete):
        run_candidate(candidate, protocol, "fictional-codex", preflight, executor=execute)
    assert len(calls) == 1
