"""Status stays a read-only API-backed command with no runtime side effects."""

import json
import socket
import sqlite3
import subprocess
from pathlib import Path

from clytie import cli
from clytie.api import local_status
from clytie.config import load_home

from .test_config import make_home


def test_status_reads_no_secrets_and_starts_no_runtime(tmp_path, monkeypatch):
    home, repository, _, _ = make_home(tmp_path)
    secret_file = home / "secrets" / "fixture.txt"
    secret_file.parent.mkdir()
    secret_file.write_text("fictional sentinel content", encoding="utf-8")
    before = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in home.rglob("*")
        if path.is_file()
    }

    def forbidden(*args, **kwargs):
        raise AssertionError("status attempted a runtime side effect")

    original_read = Path.read_text

    def checked_read(path, *args, **kwargs):
        assert "secrets" not in path.parts
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Path, "read_text", checked_read)
    report = local_status(home, repo_root=repository)
    assert report.valid, report.to_dict()
    after = {
        path: (path.stat().st_size, path.stat().st_mtime_ns)
        for path in home.rglob("*")
        if path.is_file()
    }
    assert before == after


def test_cli_uses_api_and_json_matches_report(tmp_path, monkeypatch, capsys):
    home, repository, _, _ = make_home(tmp_path)
    report = load_home(home, repo_root=repository)
    calls = []

    def api(home_argument):
        calls.append(home_argument)
        return report

    monkeypatch.setattr(cli, "local_status", api)
    assert cli.main(["status", "--home", str(home), "--json"]) == 0
    assert calls == [str(home)]
    assert json.loads(capsys.readouterr().out) == report.to_dict()


def test_cli_exit_codes_and_text_errors(tmp_path, monkeypatch, capsys):
    home, repository, _, _ = make_home(tmp_path)
    for selected, expected in ((home, 0), (tmp_path, 1), (tmp_path / "missing", 2)):
        monkeypatch.setattr(cli, "local_status", lambda path: load_home(path, repo_root=repository))
        assert cli.main(["status", "--home", str(selected)]) == expected
        text = capsys.readouterr().out
        assert str(selected) in text
        if expected:
            assert "期望" in text and "实际" in text


def test_symlink_configuration_is_rejected_before_read(tmp_path, monkeypatch):
    home, repository, _, _ = make_home(tmp_path)
    target = home / "clytie.local.yaml"
    original_read = Path.read_text
    original_symlink = Path.is_symlink

    def checked_read(path, *args, **kwargs):
        assert path != target
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or original_symlink(path))
    monkeypatch.setattr(Path, "read_text", checked_read)
    report = local_status(home, repo_root=repository)
    assert not report.valid
    assert any(error.actual == "restricted path" for error in report.errors)


def test_secrets_directory_is_not_read_as_configuration(tmp_path, monkeypatch):
    home, repository, _, _ = make_home(tmp_path)
    secret_home = home / "secrets"
    secret_home.mkdir()
    (secret_home / "clytie.local.yaml").write_text("unread fictional sentinel", encoding="utf-8")
    original_read = Path.read_text

    def checked_read(path, *args, **kwargs):
        assert "secrets" not in path.parts
        return original_read(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", checked_read)
    report = local_status(secret_home, repo_root=repository)
    assert not report.valid
    assert any(error.actual == "restricted path" for error in report.errors)
