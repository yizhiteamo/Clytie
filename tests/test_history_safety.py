"""用内存中的 Git 输出检查历史策略，不创建仓库、数据库或私有样本。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("history_safety", ROOT / "scripts/audit_history.py")
assert SPEC and SPEC.loader
AUDIT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(AUDIT)

CURRENT_COMMIT = "a" * 40
OLD_COMMIT = "b" * 40
BLOB_ID = "c" * 40
REGULAR_MODE = format(0o100_644, "o")
SYMLINK_MODE = format(0o120_000, "o")
SUBMODULE_MODE = format(0o160_000, "o")
SAFE_METADATA = (
    b"fictional-author\nowner@example.invalid\nfictional-author\nowner@example.invalid\nfixture\n"
)


def run_history(
    monkeypatch,
    capsys,
    *,
    path=b"docs/removed.txt",
    content=b"Fictional historical document.\n",
    mode=REGULAR_MODE,
    kind="blob",
    size=None,
    metadata=SAFE_METADATA,
    repeat=False,
    current_mode=None,
    denylist="private-example-token\n",
):
    original_read = Path.read_text

    def read_text(file, **kwargs):
        if file.name == "private-denylist.txt":
            return denylist
        assert file == ROOT / ".pre-commit-config.yaml"
        return original_read(file, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    calls = []
    entry = f"{mode} {kind} {BLOB_ID}\t".encode() + path + b"\0"

    def git(*args):
        calls.append(args)
        if args == ("rev-list", "--all"):
            return f"{CURRENT_COMMIT}\n{OLD_COMMIT}\n".encode()
        if args[:3] == ("show", "-s", "--format=%an%n%ae%n%cn%n%ce%n%B"):
            return metadata
        if args == ("ls-tree", "-r", "-z", CURRENT_COMMIT):
            if current_mode is not None:
                return f"{current_mode} {kind} {BLOB_ID}\t".encode() + path + b"\0"
            return entry if repeat else b""
        if args == ("ls-tree", "-r", "-z", OLD_COMMIT):
            return entry
        if args == ("cat-file", "-s", BLOB_ID):
            return str(len(content) if size is None else size).encode()
        if args == ("cat-file", "blob", BLOB_ID):
            return content
        raise AssertionError(f"unexpected git operation: {args[0]}")

    monkeypatch.setattr(AUDIT, "git", git)
    code = AUDIT.main()
    return code, capsys.readouterr().out, calls


@pytest.mark.parametrize(
    ("path", "category"),
    [
        (b"memory.db", "forbidden-file-type"),
        (b"secrets/removed.txt", "runtime-directory"),
        (b"logs/removed.txt", "runtime-directory"),
        (b"clytie.local.yaml", "deployment-file"),
        (b".env", "secret-environment-file"),
    ],
)
def test_rejects_forbidden_files_even_if_deleted_from_current_tree(
    monkeypatch, capsys, path, category
):
    code, output, calls = run_history(monkeypatch, capsys, path=path)
    assert code == 1
    assert category in output
    assert f"blob:{BLOB_ID}" in output
    assert path.decode() not in output
    assert ("ls-tree", "-r", "-z", CURRENT_COMMIT) in calls
    assert ("ls-tree", "-r", "-z", OLD_COMMIT) in calls


def test_rejects_historical_large_blob_before_loading_content(monkeypatch, capsys):
    code, output, calls = run_history(monkeypatch, capsys, size=1024 * 1024 + 1)
    assert code == 1
    assert "file-over-one-mib" in output
    assert ("cat-file", "blob", BLOB_ID) not in calls


@pytest.mark.parametrize(
    ("content", "category"),
    [(b"text\0text", "binary-content"), (b"\xff", "non-text-history-entry")],
)
def test_rejects_historical_binary_and_non_utf8_blobs(monkeypatch, capsys, content, category):
    code, output, _ = run_history(monkeypatch, capsys, content=content)
    assert code == 1
    assert category in output
    assert "docs/removed.txt" not in output


@pytest.mark.parametrize(("mode", "kind"), [(SYMLINK_MODE, "blob"), (SUBMODULE_MODE, "commit")])
def test_rejects_symlinks_and_submodules_without_opening_them(monkeypatch, capsys, mode, kind):
    code, output, calls = run_history(monkeypatch, capsys, mode=mode, kind=kind)
    assert code == 1
    assert "unsupported-history-entry" in output
    assert not any(call[0] == "cat-file" for call in calls)


def test_non_utf8_path_is_rejected_without_echoing_raw_path(monkeypatch, capsys):
    code, output, _ = run_history(monkeypatch, capsys, path=b"private-example-token\xff.txt")
    assert code == 1
    assert "non-utf8-history-path" in output
    assert "private-example-token" not in output


def test_historical_mode_is_checked_even_when_blob_and_path_were_already_scanned(
    monkeypatch, capsys
):
    code, output, _ = run_history(monkeypatch, capsys, mode=SYMLINK_MODE, current_mode=REGULAR_MODE)
    assert code == 1
    assert "unsupported-history-entry" in output


@pytest.mark.parametrize(
    "fields",
    [
        {"path": b"docs/private-example-token.txt"},
        {"content": b"private-example-token"},
        {"metadata": b"private-example-token"},
    ],
)
def test_private_matches_never_echo_source_values(monkeypatch, capsys, fields):
    code, output, _ = run_history(monkeypatch, capsys, **fields)
    assert code == 1
    assert "private-content" in output
    assert "private-example-token" not in output


def test_valid_history_is_scanned_once_per_blob_path_pair(monkeypatch, capsys):
    code, output, calls = run_history(monkeypatch, capsys, repeat=True)
    assert code == 0
    assert "2 commits, 1 unique blob/path pairs" in output
    assert "file policies" in output
    assert calls.count(("cat-file", "blob", BLOB_ID)) == 1


def test_historical_fictional_example_config_is_allowed(monkeypatch, capsys):
    code, _, _ = run_history(
        monkeypatch,
        capsys,
        path=b"examples/home/clytie.local.yaml",
        content=b"account: 1000000001\n",
    )
    assert code == 0


OFFICIAL_EMAIL = "@".join(("noreply", "github.com"))


def official_metadata(
    *,
    author_name="fictional-author",
    author_email="owner@example.invalid",
    committer_name="GitHub",
    committer_email=OFFICIAL_EMAIL,
    message="fixture",
):
    return "\n".join(
        (author_name, author_email, committer_name, committer_email, message, "")
    ).encode()


def test_exact_official_committer_is_allowed(monkeypatch, capsys):
    code, output, _ = run_history(monkeypatch, capsys, metadata=official_metadata())
    assert code == 0
    assert "History scan passed" in output


@pytest.mark.parametrize(
    "fields",
    [
        {"committer_name": "github"},
        {"committer_name": "GitHub "},
        {"committer_name": "fictional-author"},
        {"committer_email": OFFICIAL_EMAIL.upper()},
        {"committer_email": "other-" + OFFICIAL_EMAIL},
    ],
)
def test_official_committer_exception_requires_exact_name_and_email(monkeypatch, capsys, fields):
    code, output, _ = run_history(monkeypatch, capsys, metadata=official_metadata(**fields))
    assert code == 1
    assert ":committer:2: possible-personal-email" in output


def test_official_identity_in_author_field_is_not_exempt(monkeypatch, capsys):
    code, output, _ = run_history(
        monkeypatch,
        capsys,
        metadata=official_metadata(author_name="GitHub", author_email=OFFICIAL_EMAIL),
    )
    assert code == 1
    assert ":author:2: possible-personal-email" in output
    assert ":committer:2: possible-personal-email" not in output


@pytest.mark.parametrize(
    "fields",
    [
        {"metadata": official_metadata(message=OFFICIAL_EMAIL)},
        {"content": OFFICIAL_EMAIL.encode()},
        {"path": f"docs/{OFFICIAL_EMAIL}.txt".encode()},
    ],
)
def test_official_email_outside_committer_field_is_not_exempt(monkeypatch, capsys, fields):
    code, output, _ = run_history(monkeypatch, capsys, **fields)
    assert code == 1
    assert "possible-personal-email" in output


def test_official_committer_still_receives_private_scan(monkeypatch, capsys):
    code, output, _ = run_history(
        monkeypatch, capsys, metadata=official_metadata(), denylist="noreply\n"
    )
    assert code == 1
    assert ":committer:2: private-content" in output
    assert "possible-personal-email" not in output
