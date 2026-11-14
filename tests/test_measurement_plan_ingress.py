"""Fictional metadata only; never decrypt, construct fixtures, or launch a trial."""

import copy
import importlib.util
import subprocess
from pathlib import Path

import pytest
from tests.measurement_tools import load as load_tool
from tests.test_measurement_plan import inputs, profiles

plan = load_tool("measurement_plan")


@pytest.fixture(autouse=True)
def no_external_commands(monkeypatch):
    original = subprocess.run
    allowed = {
        "init",
        "config",
        "add",
        "commit",
        "rev-parse",
        "ls-tree",
        "cat-file",
        "diff-tree",
        "rev-list",
        "archive",
        "show",
    }

    def run(arguments, *args, **kwargs):
        if type(arguments) is not list or arguments[0] != "git" or arguments[1] not in allowed:
            raise AssertionError("nonlocal command forbidden in ingress tests")
        return original(arguments, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)


def load():
    load_tool("measurement_plan_ingress")
    spec = importlib.util.spec_from_file_location(
        "ingress_under_test",
        Path(__file__).resolve().parents[1] / "scripts/measurement_plan_ingress.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def setup(tmp_path, monkeypatch):
    m = load()
    repo = tmp_path / "repository"
    repo.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo).decode().strip()

    git("init", "-q")
    git("config", "user.name", "fictional")
    git("config", "user.email", "fictional@example.com")
    git("config", "commit.gpgsign", "false")
    git("config", "core.autocrlf", "false")
    git("config", "core.hooksPath", "disabled-fictional-hooks")
    (repo / "product.txt").write_text("fictional product")

    def commit(message):
        git("add", ".")
        git("commit", "-qm", message)
        return git("rev-parse", "HEAD")

    candidate = commit("product")
    (repo / "tool.txt").write_text("fictional tool")
    tool = commit("tool")
    inv, geometry = inputs()
    basis = tmp_path / "basis.txt"
    basis.write_text("fictional engineering validation only")
    reference = m.sha(basis.read_bytes())
    inv.update(
        candidate=candidate,
        tool_commit=tool,
        source_inventory_sha256="f" * 64,
        verification_sha256=reference,
    )
    cost = profiles()
    for platform in cost.values():
        for profile in platform.values():
            profile["basis"] = reference
            for n, phase in enumerate(profile["phases"], 1):
                phase.update(name=f"phase_{n:02d}", basis=reference)
            for reserve in profile["reserves"].values():
                reserve["basis"] = reference
    frozen = plan.freeze_plan(plan.draft_plan("mechanism", inv, geometry, profiles=cost))
    payload = dict(
        format=1, purpose=m.PURPOSE, tool_commit=tool, thresholds=m.THRESHOLDS, plan=frozen
    )
    source = tmp_path / "input.json"
    source.write_bytes(m.pack(payload))
    checks = []
    monkeypatch.setattr(m, "materials", lambda *args: checks.append("materials"))
    scans = []
    monkeypatch.setattr(
        m.evidence,
        "private_scan",
        lambda paths: scans.append([Path(p).read_bytes() for p in paths]) or "pass",
    )
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.setattr(
        m.base, "repository", lambda: dict(full_name=m.base.REPOSITORY, private=True)
    )
    monkeypatch.setattr(m, "CAMPAIGN_ROOT", tmp_path / "campaign")
    local = tmp_path / "local"
    m.record_local(source, local, repo, {}, {reference: basis})
    for name, data in (
        (m.PLAN_PATH, (local / "plan.json").read_bytes()),
        (m.RECORD_PATH, (local / "verification.json").read_bytes()),
    ):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    p = commit("plan")
    request = m.request_bytes(repo, p, local)
    (repo / m.REQUEST_PATH).write_bytes(request)
    q = commit("request")
    run = dict(
        id=123,
        run_attempt=1,
        head_sha=q,
        event="pull_request",
        status="in_progress",
        path=m.WORKFLOW_PATH,
        repository=dict(full_name=m.base.REPOSITORY),
        head_repository=dict(full_name=m.base.REPOSITORY),
    )

    def repository():
        return dict(full_name=m.base.REPOSITORY, private=True)

    env = dict(
        GITHUB_ACTIONS="true",
        GITHUB_REPOSITORY=m.base.REPOSITORY,
        GITHUB_RUN_ID="123",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_JOB="mechanism_linux",
        GITHUB_EVENT_NAME="pull_request",
        GITHUB_REPOSITORY_ID="7",
        GITHUB_WORKFLOW_REF=m.base.REPOSITORY + "/" + m.WORKFLOW_PATH + "@refs/pull/71/merge",
    )
    event_repo = dict(id=7, full_name=m.base.REPOSITORY, private=True, fork=False)
    event = dict(
        action="synchronize",
        number=71,
        before=p,
        after=q,
        repository=copy.deepcopy(event_repo),
        pull_request=dict(
            number=71,
            state="open",
            head=dict(sha=q, repo=copy.deepcopy(event_repo)),
            base=dict(ref="main", repo=copy.deepcopy(event_repo)),
        ),
    )
    return locals()


def authorize(s):
    m = s["m"]
    raw = m.authorize_run(
        s["repo"], s["q"], s["local"], 123, repository=s["repository"], github=lambda _: s["run"]
    )
    (s["repo"] / m.ADMISSION_PATH).write_bytes(raw)
    return s["commit"]("admission")


def test_real_public_scan_fake_private_scan_and_git_bound_commits(setup):
    s = setup
    m = s["m"]
    b = authorize(s)
    scan_count = len(s["scans"])
    frozen, invocation = m.accept_ci(
        s["repo"], b, s["env"], {}, event=s["event"], repository=s["repository"]
    )
    assert len(s["scans"]) == scan_count
    assert frozen == s["frozen"]
    assert invocation["product_commit"] == s["candidate"]
    assert len({b, s["q"], s["p"], s["tool"], s["candidate"]}) == 5
    assert len(s["checks"]) >= 2 and len(s["scans"]) >= 3
    assert invocation["private_scan"] == "local_record_not_independently_verified_in_ci"


@pytest.mark.parametrize(
    "change", ["unknown", "path", "command", "url", "candidate", "number", "free_basis"]
)
def test_schema_and_scope_rejections(setup, change):
    s = setup
    m = s["m"]
    payload = copy.deepcopy(s["payload"])
    if change == "unknown":
        payload["approved"] = True
    elif change in {"path", "command", "url", "free_basis"}:
        payload["plan"]["inputs"]["profiles"]["Linux"]["large_asset"]["basis"] = {
            "path": "C:/fake/file",
            "command": "echo fictional",
            "url": "https://example.com",
            "free_basis": "fictional text",
        }[change]
    elif change == "candidate":
        payload["plan"]["inputs"]["inventory"]["candidate"] = "HEAD"
    else:
        payload["plan"]["parameters"]["waiting"] = 9
    with pytest.raises(ValueError):
        m.validate_payload(m.pack(payload))


def test_decimal_format_is_visible_exact_and_strict(setup):
    m = setup["m"]
    raw = m.pack(dict(n=128 * 1024**2, f=1024.5))
    assert b"134,217,728" in raw and b"1,024.5" in raw
    assert m.unpack(raw) == dict(n=128 * 1024**2, f=1024.5)
    for raw in (
        b'{"integer":"12,34"}',
        b'{"integer":"001"}',
        b'{"real":"1e9"}',
        b'{"integer":"1_000"}',
        b'{"integer":"+1"}',
        b'{"real":"0.' + b"123" + b'4567"}',
        b'{"real":"0.12 345 67"}',
    ):
        with pytest.raises(ValueError):
            m.unpack(raw)


def test_derived_long_fraction_is_exact_and_passes_original_public_scan(setup):
    m = setup["m"]
    value = 256 / (4 * 1024**2)
    raw = m.pack(dict(busy_transfer_seconds=value))
    assert b"0.000 061 035 156 25" in raw
    assert m.unpack(raw)["busy_transfer_seconds"] == value
    m.public_scan(raw)
    derived = plan.derive_window({"memory_database_bytes": 17}, profiles()["Linux"]["large_asset"])
    raw = m.pack(derived)
    assert plan.digest(m.unpack(raw)) == plan.digest(derived)
    m.public_scan(raw)


@pytest.mark.parametrize("failure", ["private", "public", "unknown_private", "basis"])
def test_local_failures_never_return_publishable_record(setup, failure):
    s = setup
    m = s["m"]
    if failure in {"private", "unknown_private"}:
        m.evidence.private_scan = lambda _: "fail" if failure == "private" else "unknown"
    if failure == "public":
        s["monkeypatch"].setattr(
            m.evidence, "public_scanner", lambda: lambda _: {"unknown-category"}
        )
    if failure == "basis":
        s["basis"].write_text("changed")
    with pytest.raises(ValueError):
        m.record_local(
            s["source"], s["tmp_path"] / "rejected", s["repo"], {}, {s["reference"]: s["basis"]}
        )


@pytest.mark.parametrize(
    "damage", ["bytes", "hash", "missing_record", "run", "attempt", "unknown_repo_visibility"]
)
def test_ci_binding_fail_closed(setup, damage):
    s = setup
    m = s["m"]
    b = authorize(s)
    if damage == "bytes":
        original = m.git_file
        m.git_file = lambda repo, commit, path: (
            original(repo, commit, path) + (b" " if path == m.PLAN_PATH else b"")
        )
    elif damage == "hash":
        original = m.git_file

        def changed(repo, commit, path):
            raw = original(repo, commit, path)
            if path == m.ADMISSION_PATH:
                value = m.unpack(raw)
                value["plan_file_sha256"] = "0" * 64
                return m.pack(value)
            return raw

        m.git_file = changed
    elif damage == "missing_record":
        original = m.git_file

        def missing(repo, commit, path):
            if path == m.RECORD_PATH:
                raise ValueError("missing")
            return original(repo, commit, path)

        m.git_file = missing
    elif damage in {"run", "attempt"}:
        s["env"]["GITHUB_RUN_ID" if damage == "run" else "GITHUB_RUN_ATTEMPT"] = "2"
    else:
        s["repository"] = lambda: dict(full_name=m.base.REPOSITORY, private=None)
    with pytest.raises(ValueError):
        m.accept_ci(s["repo"], b, s["env"], {}, event=s["event"], repository=s["repository"])


@pytest.mark.parametrize(
    "change",
    [
        "event",
        "action",
        "after",
        "head",
        "fork",
        "private",
        "repo",
        "pr",
        "base",
        "workflow",
        "repository_id",
    ],
)
def test_ci_event_single_field_mismatch(setup, change):
    s = setup
    if change == "event":
        s["env"]["GITHUB_EVENT_NAME"] = "push"
    elif change == "action":
        s["event"]["action"] = "opened"
    elif change == "after":
        s["event"]["after"] = s["p"]
    elif change == "head":
        s["event"]["pull_request"]["head"]["sha"] = s["p"]
    elif change in {"fork", "private", "repo"}:
        key, value = {
            "fork": ("fork", True),
            "private": ("private", None),
            "repo": ("full_name", "fictional/other"),
        }[change]
        s["event"]["pull_request"]["head"]["repo"][key] = value
    elif change == "pr":
        s["event"]["pull_request"]["number"] = 72
    elif change == "base":
        s["event"]["pull_request"]["base"]["ref"] = "other"
    elif change == "workflow":
        s["env"]["GITHUB_WORKFLOW_REF"] = "fictional/other"
    else:
        s["env"]["GITHUB_REPOSITORY_ID"] = "8"
    with pytest.raises(ValueError):
        s["m"].check_ci_event(s["event"], s["env"], s["q"], s["p"])


@pytest.mark.parametrize("variable", ["CI", "GITHUB_ACTIONS"])
def test_local_entrypoints_reject_each_ci_flag(setup, variable):
    s = setup
    s["monkeypatch"].setenv(variable, "true")
    for call in (
        lambda: s["m"].record_local(
            s["source"], s["tmp_path"] / "blocked", s["repo"], {}, {s["reference"]: s["basis"]}
        ),
        lambda: s["m"].request_bytes(s["repo"], s["p"], s["local"]),
        lambda: s["m"].authorize_run(
            s["repo"],
            s["q"],
            s["local"],
            123,
            repository=s["repository"],
            github=lambda _: s["run"],
        ),
    ):
        with pytest.raises(ValueError, match="local_only_operation"):
            call()


def test_campaign_never_resets_for_new_plan_or_run(setup):
    s = setup
    authorize(s)
    marker = s["m"].CAMPAIGN_ROOT / "admission.json"
    before = marker.read_bytes()
    with pytest.raises((FileExistsError, ValueError)):
        s["m"].authorize_run(
            s["repo"],
            s["q"],
            s["local"],
            123,
            repository=s["repository"],
            github=lambda _: s["run"],
        )
    assert marker.read_bytes() == before


def test_raw_evidence_and_execution_are_not_in_transport(setup):
    m = setup["m"]
    assert m.CAMPAIGN == "a43_issue72_mechanism_round_one"
    assert "launch_one" not in Path(m.__file__).read_text()
    with pytest.raises(ValueError):
        m.git_file(setup["repo"], setup["p"], "raw/geometry.json")


def test_unselected_profile_is_still_strictly_whitelisted(setup):
    s = setup
    payload = copy.deepcopy(s["payload"])
    frozen = payload["plan"]
    frozen["inputs"]["profiles"]["Linux"]["small_text"]["unknown"] = "fictional_command"
    payload["plan"] = plan.freeze_plan(plan.draft_plan(**frozen["inputs"]))
    with pytest.raises(ValueError):
        s["m"].validate_payload(s["m"].pack(payload))


def test_thresholds_and_implicit_tool_version_are_rejected(setup):
    s = setup
    for change in ("threshold", "tool"):
        payload = copy.deepcopy(s["payload"])
        if change == "threshold":
            payload["thresholds"]["control_p99_ms"] = 101
        else:
            del payload["plan"]["inputs"]["inventory"]["tool_commit"]
            payload["plan"] = plan.freeze_plan(plan.draft_plan(**payload["plan"]["inputs"]))
        with pytest.raises(ValueError):
            s["m"].validate_payload(s["m"].pack(payload))


def test_second_observed_run_cannot_consume_same_campaign(setup):
    s = setup
    authorize(s)
    s["run"]["id"] = 124
    with pytest.raises(FileExistsError):
        s["m"].authorize_run(
            s["repo"],
            s["q"],
            s["local"],
            124,
            repository=s["repository"],
            github=lambda _: s["run"],
        )


def test_changed_plan_has_no_new_campaign_budget(setup):
    s = setup
    m = s["m"]
    authorize(s)
    marker = m.CAMPAIGN_ROOT / "admission.json"
    before = marker.read_bytes()
    payload = copy.deepcopy(s["payload"])
    t = s["git"]("rev-parse", "HEAD")
    payload["tool_commit"] = t
    payload["plan"]["inputs"]["inventory"]["tool_commit"] = t
    payload["plan"] = plan.freeze_plan(plan.draft_plan(**payload["plan"]["inputs"]))
    assert payload["plan"]["plan_sha256"] != s["frozen"]["plan_sha256"]
    source = s["tmp_path"] / "new-input.json"
    source.write_bytes(m.pack(payload))
    local = s["tmp_path"] / "new-local"
    m.record_local(source, local, s["repo"], {}, {s["reference"]: s["basis"]})
    for filename, name in ((m.PLAN_PATH, "plan.json"), (m.RECORD_PATH, "verification.json")):
        (s["repo"] / filename).write_bytes((local / name).read_bytes())
    p = s["commit"]("new plan")
    (s["repo"] / m.REQUEST_PATH).write_bytes(m.request_bytes(s["repo"], p, local))
    q = s["commit"]("new request")
    s["run"].update(id=124, head_sha=q)
    with pytest.raises(FileExistsError):
        m.authorize_run(
            s["repo"], q, local, 124, repository=s["repository"], github=lambda _: s["run"]
        )
    assert marker.read_bytes() == before


def test_actual_material_bytes_are_separate_from_current_product_head(setup):
    s = setup
    actual = load()
    names = (
        actual.TOOL_PATH,
        actual.WORKFLOW_PATH,
        "scripts/measurement_fixture.py",
        "scripts/measurement_geometry.py",
        "scripts/measurement_preparation_evidence.py",
        "scripts/measurement_preparation_transport.py",
    )
    for name in names:
        path = s["repo"] / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            Path(actual.__file__).read_bytes() if name == actual.TOOL_PATH else b"fictional tool"
        )
    t = s["commit"]("fixture tool inventory")
    s["monkeypatch"].setattr(actual.launch, "TOOL_FILES", names)
    s["monkeypatch"].setattr(actual.versions, "LEGACY", s["candidate"])
    s["monkeypatch"].setattr(actual.versions, "ORIGINAL", s["candidate"])
    directories = {}
    for mode in ("candidate", "legacy", "original"):
        directory = s["tmp_path"] / ("source_" + mode)
        actual.versions.prepare(
            s["repo"], directory, mode=mode, candidate=s["candidate"], tool_commit=t
        )
        directories[mode] = directory
    value = copy.deepcopy(s["payload"])
    value["tool_commit"] = t
    inv = value["plan"]["inputs"]["inventory"]
    tools = actual.launch.tool_inventory(s["repo"])
    inv.update(
        tool_commit=t,
        tool_sha256=tools["sha256"],
        fixture_sha256=tools["files"]["scripts/measurement_fixture.py"],
        source_inventory_sha256=plan.digest(actual.versions.verify(directories["candidate"])),
    )
    actual.materials(s["repo"], value, directories)
    assert s["git"]("rev-parse", "HEAD") == t != s["candidate"]
    (s["repo"] / "scripts/measurement_fixture.py").write_bytes(b"changed fictional tool")
    with pytest.raises(ValueError):
        actual.materials(s["repo"], value, directories)


def test_public_same_repository_fixed_admission_remains_required(setup):
    s = setup
    s["repository"] = lambda: dict(full_name=s["m"].base.REPOSITORY, private=False)
    for repo in (
        s["event"]["repository"],
        s["event"]["pull_request"]["head"]["repo"],
        s["event"]["pull_request"]["base"]["repo"],
    ):
        repo["private"] = False
    b = authorize(s)
    result, _ = s["m"].accept_ci(
        s["repo"], b, s["env"], {}, event=s["event"], repository=s["repository"]
    )
    assert result == s["frozen"]
