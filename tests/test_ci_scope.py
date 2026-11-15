import json
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCOPE = runpy.run_path(str(ROOT / "scripts/ci_scope.py"))


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        (["README.md", "AGENTS.md", "docs/design/topic.md"], "docs"),
        (["docs/image.svg", "docs/nested/schema.json"], "docs"),
        (["docs/architecture/agent-spec.md"], "full"),
        (["README.md", "src/clytie/main.py"], "full"),
        (["plugins/example/README.md"], "full"),
        ([], "full"),
        ([".github/workflows/ci.yml"], "full"),
        (["tests/README.md"], "full"),
        (["scripts/example.md"], "full"),
        (["schemas/example.md"], "full"),
        (["examples/README.md"], "full"),
        (["uv.lock"], "full"),
        (["pyproject.toml"], "full"),
        (["docs/../src/code.py"], "full"),
        (["/docs/topic.md"], "full"),
        (["docs\\topic.md"], "full"),
        (["docs/"], "full"),
        ([None], "full"),
        ("README.md", "full"),
    ],
)
def test_classify_changed_files(files, expected):
    assert SCOPE["classify_files"](files) == expected


def test_repository_documents_mentioned_in_tests_require_full_ci():
    documents = sorted(
        path.relative_to(ROOT).as_posix() for path in (ROOT / "docs").rglob("*") if path.is_file()
    )
    test_roots = [ROOT / "tests", *sorted((ROOT / "plugins").glob("*/tests"))]
    missing = []
    for test_root in test_roots:
        for source in sorted(test_root.rglob("*.py")):
            for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                for document in documents:
                    if document in line and document not in SCOPE["TEST_DOCUMENTS"]:
                        missing.append(
                            f"{document}: {source.relative_to(ROOT).as_posix()}:{line_number}"
                        )
    assert not missing, "Add test document dependencies to TEST_DOCUMENTS:\n" + "\n".join(missing)


def test_cli_file_list_and_invalid_input():
    for value, expected in [(' ["docs/topic.md"] ', "docs"), ("invalid", "full")]:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts/ci_scope.py")],
            input=value,
            text=True,
            capture_output=True,
            check=True,
        )
        assert result.stdout.strip() == expected


def test_github_compare_fails_closed_and_checks_both_rename_paths(tmp_path):
    github_scope = SCOPE["github_scope"]

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path).decode().strip()

    git("init", "-q")
    git("config", "user.name", "Fictional Test")
    git("config", "user.email", "test@example.com")
    (tmp_path / "code.py").write_text("fictional\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    (tmp_path / "README.md").write_text("fictional docs\n")
    git("add", ".")
    git("commit", "-qm", "docs")
    head = git("rev-parse", "HEAD")
    event = tmp_path / "event.json"

    def scope(start=base, end=head, event_name="pull_request"):
        event.write_text(
            json.dumps({"pull_request": {"base": {"sha": start}, "head": {"sha": end}}})
        )
        return github_scope(event_name, event, tmp_path)

    assert scope() == "docs"
    assert scope(event_name="push") == "full"
    assert scope(start="f" * 40) == "full"
    assert scope(end=base) == "full"
    assert scope(start="--help") == "full"
    assert github_scope("pull_request", tmp_path / "absent", tmp_path) == "full"
    event.write_text("{}")
    assert github_scope("pull_request", event, tmp_path) == "full"
    git("mv", "code.py", "code.md")
    git("commit", "-qm", "rename code into documentation")
    assert scope(end=git("rev-parse", "HEAD")) == "full"


def test_workflow_keeps_required_matrix_and_gates_only_post_scan_steps():
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert workflow.get("on", workflow.get(True)) == {"pull_request": None}
    job = workflow["jobs"]["offline"]
    assert job["name"] == "offline (${{ matrix.os }}, Python ${{ matrix.python }})"
    assert job["strategy"]["matrix"] == {
        "os": ["ubuntu-latest", "windows-latest"],
        "python": ["3.10"],
    }
    steps = job["steps"]
    scan = next(i for i, s in enumerate(steps) if s.get("name") == "Public content scan")
    assert all("if" not in s for s in steps[: scan + 1])
    assert all(s.get("if") == "steps.scope.outputs.scope != 'docs'" for s in steps[scan + 1 :])
    scope = next(s for s in steps if s.get("id") == "scope")
    assert "scope=full" in scope["run"]
    assert "--github" in scope["run"]
