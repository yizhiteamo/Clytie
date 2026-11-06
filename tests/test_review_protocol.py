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


def test_review_client_environment_excludes_private_locators():
    client_environment = codex_review.client_environment
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


def test_external_preflight_cannot_start_exec_or_consume_round(tmp_path):
    protocol = ReviewProtocol(tmp_path / "reviews", 1)
    calls = []
    with pytest.raises(ReviewIncomplete, match="same-instance"):
        codex_review.run_candidate(
            tmp_path, protocol, "fictional-codex", {"passed": True}, executor=calls.append
        )
    assert not calls
    assert protocol.rounds() == []
