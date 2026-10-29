"""A3.2 配置范围与跨字段约束；只用临时虚构实例。"""

import pytest

from clytie.config import load_home

from .test_config import make_home, write_yaml


@pytest.mark.parametrize(
    "field,value",
    [
        ("agent.max_tool_rounds", 0),
        ("agent.max_tool_rounds", 9),
        ("agent.model_call_seconds", 9),
        ("agent.model_call_seconds", 601),
        ("replies.fallback_private", "x" * 201),
        ("tasks.cancel_words", []),
        ("tasks.cancel_words", ["x" * 11]),
        ("boundary.pause_hours", 0),
        ("boundary.input_filter.words", ["x" * 101]),
        ("boundary.input_filter.patterns", ["["]),
        ("boundary.input_filter.patterns", ["x" * 201]),
        ("outbound.style_words", ["x" * 51]),
        ("protection.enter_ratio", 0.4),
        ("protection.exit_ratio", 0.2),
        ("names.internal_scenes", ["public_group"]),
        ("names.internal_scenes", []),
    ],
)
def test_agent_config_reports_file_and_field(tmp_path, field, value):
    home, repo, _, companion = make_home(tmp_path)
    parts = field.split(".")
    target = companion
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = value
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    assert not report.valid
    assert any(e.file.endswith("companion.yaml") for e in report.errors)
    pointer = "/" + field.replace(".", "/")
    if field == "protection.enter_ratio":
        pointer = "/protection/exit_ratio"
    assert any(e.pointer.startswith(pointer) for e in report.errors) or (
        field == "names.internal_scenes"
        and any(e.pointer.startswith("/memory/private_scenes") for e in report.errors)
    )


def test_agent_config_accepts_empty_fallback_and_distinct_ratios(tmp_path):
    home, repo, _, companion = make_home(tmp_path)
    companion["replies"] = {"fallback_private": ""}
    companion["protection"] = {"enter_ratio": 0, "exit_ratio": 1}
    companion["boundary"] = {"input_filter": {"patterns": ["fictional.*"]}}
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    assert report.valid, report.to_dict()
