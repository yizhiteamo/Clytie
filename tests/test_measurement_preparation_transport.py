"""Transport regressions: fake GitHub/CI; fictional fixtures and in-memory age keys."""

import io
import json
import stat
import tarfile
import types
import zipfile
from pathlib import Path

import pytest
from tests.measurement_tools import load
from tests.test_measurement_preparation_evidence import fixture

transport = load("measurement_preparation_transport")


def test_entrypoints_exist():
    for name in ("validate_sealed", "upload_gate", "download", "verify", "ci_seal"):
        assert callable(getattr(transport, name, None)), name


def sealed(tmp_path):
    source, binding, contents, identity = fixture(tmp_path)
    target = tmp_path / "sealed"
    transport.prep.seal(source, target, str(identity.to_public()), binding)
    return target, binding, contents, identity


def fake_private():
    return dict(full_name=transport.base.REPOSITORY, private=True)


def metadata():
    return dict(
        id=42,
        name="a43-preparation-encrypted-fictional",
        expired=False,
        workflow_run=dict(id=123),
        created_at="2026-01-01T00:00:00Z",
        expires_at="2026-01-15T00:00:00Z",
    )


def archive_of(directory, *, extra=None, mode=None):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in directory.iterdir():
            info = zipfile.ZipInfo(path.name)
            if mode is not None:
                info.external_attr = mode << 16
            archive.writestr(info, path.read_bytes())
        if extra:
            archive.writestr(extra, b"x")
    return output.getvalue()


def test_upload_returns_exact_paths(tmp_path):
    directory, *_ = sealed(tmp_path)
    assert transport.upload_gate(directory, repository=fake_private) == [
        str(directory / "preparation.age"),
        str(directory / "receipt.json"),
    ]


@pytest.mark.parametrize("change", ["extra", "hash", "purpose", "format", "size", "type"])
def test_upload_rejects_invalid_material(tmp_path, change):
    directory, *_ = sealed(tmp_path)
    receipt_path = directory / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    if change == "extra":
        (directory / "raw.json").write_text("{}")
    else:
        key, value = {
            "hash": ("ciphertext_sha256", "0" * 64),
            "purpose": ("purpose", "measurement"),
            "format": ("format", 1),
            "size": ("ciphertext_bytes", 0),
            "type": ("ciphertext_bytes", True),
        }[change]
        receipt[key] = value
        receipt_path.write_text(json.dumps(receipt))
    with pytest.raises(transport.Error):
        transport.validate_sealed(directory)


@pytest.mark.parametrize("private", [False, None, "true"])
def test_visibility_fails_closed(tmp_path, private):
    directory, *_ = sealed(tmp_path)
    with pytest.raises(transport.Error):
        transport.upload_gate(
            directory, repository=lambda: dict(full_name=transport.base.REPOSITORY, private=private)
        )


def test_download_pending_and_original_hash(tmp_path, monkeypatch):
    directory, *_ = sealed(tmp_path)
    raw = archive_of(directory)
    calls = []
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    def fetch(endpoint, target, *, max_bytes):
        calls.append(max_bytes)
        target.write_bytes(raw)

    state = transport.download(
        42,
        123,
        metadata()["name"],
        tmp_path / "download",
        repository=fake_private,
        github=lambda endpoint: metadata(),
        downloader=fetch,
    )
    assert state["state"] == "encrypted_pending_verification"
    assert state["downloaded_sha256"] == transport.prep.digest(raw)
    assert state["local_archive_verified"] is False
    assert calls == [transport.MAX_DOWNLOAD]
    assert (tmp_path / "download" / "artifact.zip").read_bytes() == raw


@pytest.mark.parametrize("bad", ["extra", "traversal", "symlink", "retention", "run", "name"])
def test_download_rejects_bad_zip_or_identity(tmp_path, monkeypatch, bad):
    directory, *_ = sealed(tmp_path)
    meta = metadata()
    if bad == "retention":
        meta["expires_at"] = "2026-01-16T00:00:00Z"
    if bad == "run":
        meta["workflow_run"]["id"] = 124
    if bad == "name":
        meta["name"] = "wrong"
    raw = archive_of(
        directory,
        extra={"extra": "other", "traversal": "../other"}.get(bad),
        mode=stat.S_IFLNK if bad == "symlink" else None,
    )
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    with pytest.raises(transport.Error):
        transport.download(
            42,
            123,
            metadata()["name"],
            tmp_path / "download",
            repository=fake_private,
            github=lambda endpoint: meta,
            downloader=lambda endpoint, target, **kw: target.write_bytes(raw),
        )


def test_verify_uses_existing_private_scanner(tmp_path, monkeypatch):
    directory, binding, contents, identity = sealed(tmp_path)
    seen = []
    monkeypatch.setattr(transport.base, "load_identity", lambda path: identity)
    monkeypatch.setattr(
        transport.evidence, "private_scan", lambda paths: seen.extend(paths) or "pass"
    )
    state = transport.verify(directory, tmp_path / "opened", Path("fictional-key"), binding)
    assert state["accepted"] is True
    assert state["measurement_acceptance"] == "not_applicable"
    assert {path.name for path in seen} == {*contents, "archive-manifest.json"}


@pytest.mark.parametrize("outcome", ["fail", "unavailable", "unknown"])
def test_verify_scanner_cannot_be_skipped(tmp_path, monkeypatch, outcome):
    directory, binding, _, identity = sealed(tmp_path)
    monkeypatch.setattr(transport.base, "load_identity", lambda path: identity)
    monkeypatch.setattr(transport.evidence, "private_scan", lambda paths: outcome)
    state = transport.verify(directory, tmp_path / "opened", Path("fictional-key"), binding)
    assert state["accepted"] is False


def test_no_overwrite_and_hardlinks(tmp_path, monkeypatch):
    directory, *_ = sealed(tmp_path)
    original = Path.stat

    def linked(path, **kwargs):
        result = original(path, **kwargs)
        if path == directory / "preparation.age":
            return types.SimpleNamespace(
                st_mode=result.st_mode, st_nlink=2, st_size=result.st_size, st_file_attributes=0
            )
        return result

    monkeypatch.setattr(Path, "stat", linked)
    with pytest.raises(transport.Error):
        transport.validate_sealed(directory)


def ci_fixture(tmp_path, monkeypatch):
    source, binding, _, identity = fixture(tmp_path)
    root = tmp_path / "repository"
    root.mkdir()
    names = (
        "scripts/measurement_fixture.py",
        "scripts/measurement_geometry.py",
        "scripts/measurement_preparation_evidence.py",
        "scripts/measurement_preparation_transport.py",
    )
    for name in names:
        path = root / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes((name + "\n").encode())
    monkeypatch.setattr(transport.launch, "TOOL_FILES", names)
    version = tmp_path / "version"
    (version / "source").mkdir(parents=True)
    (version / "source" / "fictional.py").write_bytes(b"fictional = True\n")
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:") as bundle:
        member = tarfile.TarInfo("fictional.py")
        data = (version / "source" / "fictional.py").read_bytes()
        member.size = len(data)
        bundle.addfile(member, io.BytesIO(data))
    archive = raw.getvalue()
    candidate = binding["candidate"]
    inventory = dict(
        format=1,
        mode="candidate",
        candidate=binding["candidate"],
        source_commit=binding["candidate"],
        archive_sha256=transport.prep.digest(archive),
        files={"fictional.py": transport.prep.digest(data)},
    )
    (version / "source-inventory.json").write_bytes(transport.prep.canonical(inventory))

    def git(repository, *arguments):
        assert repository == root
        if arguments == ("rev-parse", "HEAD"):
            return candidate.encode() + b"\n"
        assert arguments == ("archive", "--format=tar", candidate)
        return archive

    monkeypatch.setattr(transport.versions, "_git", git)
    binding.update(transport.material_identity(root, version, binding["candidate"]))
    geometry_path = source / "geometry.json"
    geometry = json.loads(geometry_path.read_bytes())
    geometry["fixture_sha256"] = binding["fixture_sha256"]
    geometry_path.write_bytes(transport.prep.canonical(geometry))
    public = dict(
        format=1,
        recipient=str(identity.to_public()),
        tool="pyrage",
        version="1.4.0",
        fingerprint=transport.evidence.fingerprint(str(identity.to_public())),
    )
    env = dict(
        GITHUB_ACTIONS="true",
        GITHUB_REPOSITORY=transport.base.REPOSITORY,
        GITHUB_RUN_ID="123",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_JOB="prepare_windows",
    )
    return source, binding, public, env, root, version


def ci_call(tmp_path, monkeypatch, *, change=None):
    source, binding, public, env, root, version = ci_fixture(tmp_path, monkeypatch)
    if change:
        change(binding, public, env, root, version)
    return transport.ci_seal(
        source,
        tmp_path / "sealed",
        public,
        binding,
        version,
        root=root,
        environ=env,
        system="Windows",
        repository=fake_private,
    )


def test_ci_checks_real_material_before_exposing_paths(tmp_path, monkeypatch):
    assert len(ci_call(tmp_path, monkeypatch)) == 2


@pytest.mark.parametrize(
    "field",
    [
        "candidate",
        "source_inventory_sha256",
        "tool_sha256",
        "geometry_tool_sha256",
        "preparation_tool_sha256",
        "fixture_sha256",
    ],
)
def test_ci_rejects_self_claimed_binding(tmp_path, monkeypatch, field):
    def change(binding, public, env, root, version):
        binding[field] = "0" * (40 if field == "candidate" else 64)

    with pytest.raises(transport.Error):
        ci_call(tmp_path, monkeypatch, change=change)
    assert not (tmp_path / "sealed").exists()


@pytest.mark.parametrize(
    "field,value",
    [
        ("GITHUB_REPOSITORY", "other/repository"),
        ("GITHUB_RUN_ID", "124"),
        ("GITHUB_RUN_ATTEMPT", "2"),
        ("GITHUB_JOB", "prepare_linux"),
        ("GITHUB_ACTIONS", "false"),
    ],
)
def test_ci_actual_run_mismatch(tmp_path, monkeypatch, field, value):
    with pytest.raises(transport.Error):
        ci_call(tmp_path, monkeypatch, change=lambda b, p, env, r, v: env.update({field: value}))
    assert not (tmp_path / "sealed").exists()


def test_ci_rejects_consistent_but_fabricated_inventory(tmp_path, monkeypatch):
    source, binding, public, env, root, version = ci_fixture(tmp_path, monkeypatch)
    path = version / "source-inventory.json"
    inventory = json.loads(path.read_bytes())
    (version / "source" / "fictional.py").write_bytes(b"changed\n")
    inventory["files"]["fictional.py"] = transport.prep.digest(b"changed\n")
    path.write_bytes(transport.prep.canonical(inventory))
    binding["source_inventory_sha256"] = transport.prep.plan.digest(inventory)
    with pytest.raises(transport.Error):
        transport.ci_seal(
            source,
            tmp_path / "sealed",
            public,
            binding,
            version,
            root=root,
            environ=env,
            system="Windows",
            repository=fake_private,
        )


def test_missing_transport_in_tool_inventory_fails(tmp_path, monkeypatch):
    _, binding, _, _, root, version = ci_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(transport.launch, "TOOL_FILES", transport.launch.TOOL_FILES[:-1])
    with pytest.raises(transport.Error):
        transport.material_identity(root, version, binding["candidate"])


@pytest.mark.parametrize("failure", ["cancel", "visibility", "changed"])
def test_ci_failures_never_return_upload_paths(tmp_path, monkeypatch, failure):
    source, binding, public, env, root, version = ci_fixture(tmp_path, monkeypatch)
    if failure == "cancel":

        def cancel(*args):
            raise KeyboardInterrupt

        monkeypatch.setattr(transport.prep, "seal", cancel)
    if failure == "changed":
        original = transport.prep.seal

        def changed(*args):
            result = original(*args)
            (root / "scripts/measurement_fixture.py").write_bytes(b"changed\n")
            return result

        monkeypatch.setattr(transport.prep, "seal", changed)
    repository = fake_private if failure != "visibility" else lambda: {}
    with pytest.raises((transport.Error, KeyboardInterrupt)):
        transport.ci_seal(
            source,
            tmp_path / "sealed",
            public,
            binding,
            version,
            root=root,
            environ=env,
            system="Windows",
            repository=repository,
        )


def test_existing_download_target_preserved(tmp_path, monkeypatch):
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    target = tmp_path / "existing"
    target.mkdir()
    (target / "keep").write_bytes(b"original")
    with pytest.raises(FileExistsError):
        transport.download(
            42,
            123,
            metadata()["name"],
            target,
            repository=fake_private,
            github=lambda endpoint: metadata(),
        )
    assert (target / "keep").read_bytes() == b"original"


def test_nul_suffixed_zip_name_rejected(tmp_path):
    directory, *_ = sealed(tmp_path)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("preparation.age", (directory / "preparation.age").read_bytes())
        archive.writestr("receipt.jsonXXXX", (directory / "receipt.json").read_bytes())
    downloaded = tmp_path / "archive.zip"
    downloaded.write_bytes(raw.getvalue().replace(b"receipt.jsonXXXX", b"receipt.json\0abc"))
    with pytest.raises(transport.Error):
        transport._extract(downloaded, tmp_path / "extracted")


@pytest.mark.parametrize("kind", ["reparse", "duplicate", "oversize", "comment"])
def test_archive_rejects_other_unapproved_metadata(tmp_path, kind):
    directory, *_ = sealed(tmp_path)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w") as archive:
        for name in transport.SEALED:
            entry = zipfile.ZipInfo(name)
            if kind == "reparse":
                entry.external_attr = 0x400
            data = (directory / name).read_bytes()
            if kind == "oversize" and name == "receipt.json":
                data = b"x" * (transport.MAX_RECEIPT + 1)
            archive.writestr(entry, data)
        if kind == "duplicate":
            with pytest.warns(UserWarning):
                archive.writestr("receipt.json", b"{}")
        if kind == "comment":
            archive.comment = b"unexpected"
    downloaded = tmp_path / "archive.zip"
    downloaded.write_bytes(raw.getvalue())
    with pytest.raises(transport.Error):
        transport._extract(downloaded, tmp_path / "extracted")


def test_verify_mandatory_external_binding(tmp_path, monkeypatch):
    directory, binding, _, identity = sealed(tmp_path)
    monkeypatch.setattr(transport.base, "load_identity", lambda path: identity)
    monkeypatch.setattr(transport.evidence, "private_scan", lambda paths: "pass")
    binding["run_id"] += 1
    with pytest.raises(transport.Error):
        transport.verify(directory, tmp_path / "opened", Path("fictional-key"), binding)
    assert json.loads((tmp_path / "opened" / "state.json").read_bytes())["accepted"] is False


def test_local_verification_refuses_ci_before_key_load(tmp_path, monkeypatch):
    directory, binding, *_ = sealed(tmp_path)
    monkeypatch.setenv("CI", "true")

    def forbidden(path):
        pytest.fail("CI must never attempt to load a private key")

    monkeypatch.setattr(transport.base, "load_identity", forbidden)
    with pytest.raises(transport.Error, match="local_only_operation"):
        transport.verify(directory, tmp_path / "opened", Path("fictional-key"), binding)


@pytest.mark.parametrize("candidate", ["HEAD", "--output", "a" * 39, "A" * 40, None])
def test_material_entry_requires_full_candidate(tmp_path, monkeypatch, candidate):
    def forbidden(*args):
        pytest.fail("Invalid candidate must fail before reading material")

    monkeypatch.setattr(transport.prep, "_safe", forbidden)
    with pytest.raises(transport.Error):
        transport.material_identity(tmp_path, tmp_path, candidate)


@pytest.mark.parametrize("changed", [False, True])
def test_output_descriptor_checked_before_writing(tmp_path, monkeypatch, changed):
    public = tmp_path / "public.json"
    expected = tmp_path / "expected.json"
    public.write_text("{}")
    expected.write_text("{}")
    output = tmp_path / "output"
    output.write_bytes(b"existing\n")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(transport, "ci_seal", lambda *args: ["cipher", "receipt"])
    original = transport.os.fstat

    def observed(descriptor):
        info = original(descriptor)
        if changed:
            return types.SimpleNamespace(
                st_dev=info.st_dev,
                st_ino=info.st_ino + 1,
                st_nlink=info.st_nlink,
                st_mode=info.st_mode,
                st_size=info.st_size,
                st_mtime_ns=info.st_mtime_ns,
            )
        return info

    # _read_file also uses fstat, so install the hook only after input reads.
    def sealed_paths(*args):
        monkeypatch.setattr(transport.os, "fstat", observed)
        return ["cipher", "receipt"]

    monkeypatch.setattr(transport, "ci_seal", sealed_paths)
    code = transport.main(
        [
            "ci-seal",
            "--source",
            str(tmp_path),
            "--target",
            str(tmp_path),
            "--public",
            str(public),
            "--expected",
            str(expected),
            "--version-directory",
            str(tmp_path),
        ]
    )
    assert code == int(changed)
    assert output.read_bytes() == (
        b"existing\n" if changed else b"existing\nciphertext=cipher\nreceipt=receipt\n"
    )
