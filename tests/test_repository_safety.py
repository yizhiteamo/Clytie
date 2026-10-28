"""Exercise the actual hook program without writing sensitive-looking fixtures."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

CONFIG = yaml.safe_load(
    (Path(__file__).resolve().parents[1] / ".pre-commit-config.yaml").read_text(encoding="utf-8")
)
PROGRAM = shlex.split(CONFIG["repos"][0]["hooks"][0]["entry"])[2]


def run_scan(
    monkeypatch,
    capsys,
    content="safe",
    *,
    private=False,
    denylist="private-example-token\n",
    filename="fixture.txt",
    size=None,
    message=False,
):
    def read_text(path, **kwargs):
        if path.name == "private-denylist.txt":
            if denylist is None:
                raise FileNotFoundError
            return denylist
        return content

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "is_file", lambda path: True)
    monkeypatch.setattr(Path, "is_symlink", lambda path: False)
    monkeypatch.setattr(
        Path,
        "stat",
        lambda path, **kwargs: SimpleNamespace(
            st_size=len(content.encode()) if size is None else size
        ),
    )
    argv = ["scan"]
    if private:
        argv.append("--private")
    if message:
        argv.append("--message")
    monkeypatch.setattr(sys, "argv", [*argv, filename])
    with pytest.raises(SystemExit) as result:
        exec(compile(PROGRAM, "safety-hook", "exec"), {"__name__": "__main__"})
    return result.value.code, capsys.readouterr().out


@pytest.mark.parametrize("denylist", [None, "", "# only a comment\n"])
def test_private_list_is_required(monkeypatch, capsys, denylist):
    code, output = run_scan(monkeypatch, capsys, private=True, denylist=denylist)
    assert code == 1
    assert "private-scan-unavailable" in output


@pytest.mark.parametrize("message", [False, True])
def test_private_match_is_rejected_without_echo(monkeypatch, capsys, message):
    marker = "private-example-token"
    code, output = run_scan(monkeypatch, capsys, marker.upper(), private=True, message=message)
    assert code == 1
    assert "private-content" in output
    assert marker not in output.lower()


@pytest.mark.parametrize(
    "content",
    [
        "account = " + "3" + "8" * 9,
        "person" + "@" + "mail" + ".org",
        "sk-" + "x" * 32,
        "api_" + 'key = os.environ.get("TEST_KEY", "unclassified-sensitive-default")',
    ],
)
def test_rejects_sensitive_patterns(monkeypatch, capsys, content):
    assert run_scan(monkeypatch, capsys, content)[0] == 1


@pytest.mark.parametrize(
    "filename",
    [
        "storage.db-wal",
        "storage.sqlite3",
        "record.png",
        "secrets/example.json",
        "data/example.json",
        "private-denylist.txt",
        "record.zip",
    ],
)
def test_forbidden_files(monkeypatch, capsys, filename):
    assert run_scan(monkeypatch, capsys, filename=filename)[0] == 1


def test_rejects_large_file(monkeypatch, capsys):
    assert run_scan(monkeypatch, capsys, size=1024 * 1024 + 1)[0] == 1


def test_accepts_documented_fictional_data_and_source(monkeypatch, capsys):
    content = "account: 1000000001\nowner@example.invalid\n20261028\nv0.1.0\n" + "a" * 64
    assert run_scan(monkeypatch, capsys, content, filename="src/clytie/memory/core.py")[0] == 0


def test_lock_metadata_has_narrow_allowance(monkeypatch, capsys):
    content = (
        'sdist = { url = "https://files.pythonhosted.org/packages/aa/bb/'
        + "c" * 60
        + '/example-1.0.0.tar.gz", hash = "sha256:'
        + "d" * 64
        + '", size = '
        + str(1024 * 1024 + 3)
        + ', upload-time = "2026-01-01T00:00:00.123Z" }'
    )
    assert run_scan(monkeypatch, capsys, content, filename="uv.lock")[0] == 0


def test_lock_file_is_not_exempt_from_account_scan(monkeypatch, capsys):
    assert run_scan(monkeypatch, capsys, "account = " + "8" * 9, filename="uv.lock")[0] == 1


def test_schema_secret_object_opening_contains_no_credential(monkeypatch, capsys):
    key = "sec" + "ret"
    declaration = f'"{key}": {{'
    assert run_scan(monkeypatch, capsys, declaration)[0] == 0
    assert run_scan(monkeypatch, capsys, declaration, private=True, denylist=key)[0] == 1


@pytest.mark.parametrize("value", ['"unapproved-fictional-value"', '{"value": "unapproved"}'])
def test_schema_structure_exception_does_not_allow_values(monkeypatch, capsys, value):
    key = "sec" + "ret"
    assert run_scan(monkeypatch, capsys, f'"{key}": {value}')[0] == 1
