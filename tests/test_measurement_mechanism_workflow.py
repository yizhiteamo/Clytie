"""Pure orchestration tests; external commands and real trial processes are blocked."""

import copy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from tests import test_measurement_plan_ingress as ingress_tests
from tests.measurement_tools import load as load_tool
from tests.test_measurement_plan import inputs, profiles

plan = load_tool("measurement_plan")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def block_external(monkeypatch):
    original = subprocess.Popen
    allowed = {
        "init",
        "config",
        "add",
        "commit",
        "rev-parse",
        "rev-list",
        "diff-tree",
        "diff",
        "show",
        "ls-tree",
        "cat-file",
        "archive",
        "fetch",
        "checkout",
    }

    def popen(args, *rest, **kwargs):
        if (
            type(args) is not list
            or args[0] != "git"
            or args[1] not in allowed
            or any("https://" in str(item) or "http://" in str(item) for item in args)
        ):
            raise AssertionError("external command or trial process forbidden")
        return original(args, *rest, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", popen)


@pytest.fixture
def module(monkeypatch):
    load_tool("measurement_mechanism_workflow")
    return load_module(
        "mechanism_workflow_under_test",
        Path(__file__).resolve().parents[1] / "scripts/measurement_mechanism_workflow.py",
    )


def frozen_plan():
    inventory, geometry = inputs()
    inventory.update(tool_commit="b" * 40, source_inventory_sha256="d" * 64)
    return plan.freeze_plan(plan.draft_plan("mechanism", inventory, geometry, profiles=profiles()))


@pytest.fixture
def setup(module, monkeypatch, tmp_path):
    m = module
    frozen = frozen_plan()
    env = dict(
        RUNNER_TEMP=str(tmp_path),
        GITHUB_ACTIONS="true",
        GITHUB_REPOSITORY=m.transport.REPOSITORY,
        GITHUB_RUN_ID="123",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_JOB="mechanism_windows",
    )
    monkeypatch.setattr(m.platform, "system", lambda: "Windows")

    def repository():
        return dict(full_name=m.transport.REPOSITORY, private=True)

    target = m.data_root(env)
    target.mkdir()
    for path in m.sources(target).values():
        path.mkdir(parents=True)

    def expected(_frozen, execution_id, directory, *, environ):
        return dict(
            candidate=_frozen["inputs"]["inventory"]["candidate"],
            execution_id=execution_id,
            run_id=123,
            job_id="mechanism_windows",
            attempt=1,
        ), {}

    monkeypatch.setattr(m.runner, "checked", expected)
    monkeypatch.setattr(m.ingress, "accept_ci", lambda *a, **kw: (copy.deepcopy(frozen), {}))
    monkeypatch.setattr(
        m, "public_identity", lambda _: dict(recipient="fictional_public_recipient")
    )
    rows = m.selected_rows(frozen, env)
    (target / "frozen-plan.json").write_bytes(m.launch.canonical(frozen))
    (target / "ready.json").write_bytes(
        m.launch.canonical(
            dict(
                format=1,
                admission_commit="c" * 40,
                plan_sha256=frozen["plan_sha256"],
                manifests=[
                    plan.digest(expected(frozen, row["execution_id"], None, environ=env)[0])
                    for row in rows
                ],
            )
        )
    )
    calls = []

    def execute(current, execution_id, directory, trial, *, environ):
        calls.append(("execute", execution_id))
        trial.mkdir()
        (trial / "process-state.json").write_text(
            json.dumps(
                dict(
                    format=1,
                    execution_id=execution_id,
                    state="driver_exited",
                    driver_exit_confirmed=True,
                    exit_code=0,
                )
            )
        )
        raw = trial / "trial/raw"
        raw.mkdir(parents=True)
        row = next(row for row in current["runs"] if row["execution_id"] == execution_id)
        (raw / "run-manifest.json").write_text(json.dumps(dict(result=row["expected_result"])))
        return dict(exit_code=0)

    def seal(raw, target, recipient, expected):
        calls.append(("seal", expected["candidate"]))
        target.mkdir()
        (target / "measurements.age").write_bytes(b"fictional sealed output")
        (target / "receipt.json").write_text("{}")

    def gate(target, *, repository):
        value = repository()
        calls.append(("private", value["private"]))
        m.preparation._private(lambda: value)
        return [str(target / name) for name in ("measurements.age", "receipt.json")]

    monkeypatch.setattr(m.transport, "upload_gate", gate)
    return locals()


def test_wait_pending_then_fixed_b_never_calls_execution(module, monkeypatch, tmp_path):
    m = module
    q, b = "a" * 40, "b" * 40
    value = dict(tool_commit="c" * 40)
    monkeypatch.setattr(m, "probe_request", lambda *a, **kw: (q, dict(plan_commit="d" * 40)))
    monkeypatch.setattr(m.ingress, "pair", lambda *a: (value, b"", b""))
    monkeypatch.setattr(m, "tool_snapshot", lambda *a: None)
    monkeypatch.setattr(m, "binding", lambda *a: (value, dict(request_commit=q)))

    def forbidden(*a, **kw):
        raise AssertionError("measurement work reached during wait")

    monkeypatch.setattr(m.runner, "launch_one", forbidden)
    monkeypatch.setattr(m.versions, "prepare", forbidden)
    monkeypatch.setattr(m.mechanism, "register_plan", forbidden)
    observed = iter((q, b))
    slept = []
    result = m.wait_for_admission(
        tmp_path,
        {},
        {},
        repository=lambda: dict(full_name=m.transport.REPOSITORY, private=True),
        fetch=lambda *a: next(observed),
        sleep=slept.append,
    )
    assert result == dict(
        eligible="true", admission_commit=b, request_commit=q, tool_commit="c" * 40
    )
    assert slept == [m.POLL_SECONDS]


@pytest.mark.parametrize("mode", ["timeout", "network", "unknown", "skip"])
def test_wait_has_no_retry_on_network_or_unknown_admission(module, monkeypatch, tmp_path, mode):
    m = module
    q = "a" * 40
    monkeypatch.setattr(
        m, "probe_request", lambda *a, **kw: None if mode == "skip" else (q, dict(plan_commit=q))
    )
    monkeypatch.setattr(m.ingress, "pair", lambda *a: ({}, b"", b""))
    monkeypatch.setattr(m, "tool_snapshot", lambda *a: None)
    counts = []

    def fetch(*a):
        counts.append(1)
        if mode == "network":
            raise ValueError("fictional network failure")
        return q if mode == "timeout" else "b" * 40

    monkeypatch.setattr(
        m, "binding", lambda *a: (_ for _ in ()).throw(ValueError("unknown admission"))
    )
    if mode == "skip":
        assert m.wait_for_admission(tmp_path, {}, {}, fetch=fetch)["eligible"] == "false"
        assert not counts
    else:
        with pytest.raises(ValueError):
            m.wait_for_admission(tmp_path, {}, {}, fetch=fetch, sleep=lambda _: None)
        assert len(counts) == (m.POLLS if mode == "timeout" else 1)


def test_network_requires_explicit_executor_and_token_stays_out_of_argv(
    module, monkeypatch, tmp_path
):
    m = module
    with pytest.raises(ValueError, match="network_executor_not_supplied"):
        m.fetch_admission(tmp_path, dict(GH_TOKEN="fictional_token"))
    seen = []
    monkeypatch.setattr(m.versions, "_git", lambda *a: b"a" * 40)

    def execute(argv, **kw):
        seen.append((argv, kw))
        return SimpleNamespace(returncode=0)

    assert (
        m.fetch_admission(tmp_path, dict(GH_TOKEN="fictional_token"), execute=execute) == "a" * 40
    )
    argv, kw = seen[0]
    assert type(argv) is list and all("fictional_token" not in item for item in argv)
    assert "GH_TOKEN" not in kw["env"] and kw["timeout"] == m.NETWORK_SECONDS
    assert "GIT_CONFIG_VALUE_1" in kw["env"]


def test_slots_follow_frozen_order_and_publish_exact_cipher_pair(setup):
    s = setup
    m = s["m"]
    for ordinal in range(4):
        result = m.run_slot(
            s["tmp_path"],
            ordinal,
            s["env"],
            {},
            repository=s["repository"],
            execute=s["execute"],
            seal=s["seal"],
        )
        assert result["succeeded"] and len(result["paths"]) == 2
        assert [Path(path).name for path in result["paths"]] == ["measurements.age", "receipt.json"]
    assert [value for kind, value in s["calls"] if kind == "execute"] == [
        row["execution_id"] for row in s["rows"]
    ]
    before = list(s["calls"])
    with pytest.raises(FileExistsError):
        m.run_slot(
            s["tmp_path"],
            0,
            s["env"],
            {},
            repository=s["repository"],
            execute=s["execute"],
            seal=s["seal"],
        )
    assert s["calls"] == before


@pytest.mark.parametrize("failure", ["exit_code", "unknown_exit", "seal", "private", "material"])
def test_failed_raw_preserved_without_retry_or_plaintext_upload(setup, failure):
    s = setup
    m = s["m"]
    execute, seal, repository = s["execute"], s["seal"], s["repository"]

    def run(*args, **kw):
        result = execute(*args, **kw)
        if failure == "exit_code":
            state_path = args[3] / "process-state.json"
            state = json.loads(state_path.read_text())
            state["exit_code"] = 1
            state_path.write_text(json.dumps(state))
            return dict(exit_code=1)
        if failure == "unknown_exit":
            (args[3] / "process-state.json").write_text(
                json.dumps(dict(state="child_exit_unconfirmed", driver_exit_confirmed=False))
            )
            raise KeyboardInterrupt()
        if failure == "material":
            s["monkeypatch"].setattr(
                m.runner, "checked", lambda *a, **kw: (_ for _ in ()).throw(ValueError("changed"))
            )
        return result

    if failure == "seal":

        def seal(*a):
            raise ValueError("failed seal")

    if failure == "private":
        observations = iter((True, False))

        def repository():
            return dict(full_name=m.transport.REPOSITORY, private=next(observations))

    result = m.run_slot(
        s["tmp_path"], 0, s["env"], {}, repository=repository, execute=run, seal=seal
    )
    assert not result["succeeded"]
    assert bool(result["paths"]) == (failure == "exit_code")
    status = result["outcome"]
    assert (
        status["failure_stage"]
        == {
            "exit_code": "execute",
            "unknown_exit": "exit_check",
            "seal": "seal",
            "private": "upload_gate",
            "material": "material_check",
        }[failure]
    )
    assert status["driver_exit_confirmed"] == (failure != "unknown_exit")
    assert status["ciphertext_saved"] == (failure in {"exit_code", "private"})
    assert status["worker_exit"] == "not_evaluated_here"
    assert status["artifact_upload"] == "not_observed" and status["accepted"] is False
    assert (s["target"] / "slot-0/trial/raw/run-manifest.json").exists()
    assert sum(kind == "execute" for kind, _ in s["calls"]) == 1
    with pytest.raises(ValueError):
        m.run_slot(
            s["tmp_path"],
            1,
            s["env"],
            {},
            repository=s["repository"],
            execute=execute,
            seal=s["seal"],
        )


@pytest.mark.parametrize("damage", ["ready", "plan", "slot_order", "identity"])
def test_no_formal_entry_before_all_preconditions(setup, damage):
    s = setup
    m = s["m"]
    if damage == "ready":
        (s["target"] / "ready.json").write_text("{}")
    elif damage == "plan":
        (s["target"] / "frozen-plan.json").write_text("{}")
    elif damage == "identity":
        s["monkeypatch"].setattr(
            m.ingress, "accept_ci", lambda *a, **kw: (_ for _ in ()).throw(ValueError("rejected"))
        )
    with pytest.raises((ValueError, OSError)):
        m.run_slot(
            s["tmp_path"],
            1 if damage == "slot_order" else 0,
            s["env"],
            {},
            repository=s["repository"],
            execute=s["execute"],
            seal=s["seal"],
        )
    assert not s["calls"]


def test_output_never_accepts_plaintext_path_by_plan_field(module, tmp_path):
    path = tmp_path / "output.txt"
    path.write_text("")
    with pytest.raises(ValueError):
        module.output(dict(GITHUB_OUTPUT=str(path)), dict(raw="fictional.json"))
    assert not path.read_text()


@pytest.fixture
def git_case(module, tmp_path, monkeypatch):
    value = ingress_tests.setup.__wrapped__(tmp_path, monkeypatch)
    monkeypatch.setattr(module, "ingress", value["m"])
    value["event"]["pull_request"]["head"]["ref"] = module.BRANCH
    return value


def test_real_git_request_admission_and_normal_push_skip(module, git_case):
    m, s = module, git_case
    assert m.probe_request(s["repo"], s["env"], s["event"], repository=s["repository"])[0] == s["q"]
    b = ingress_tests.authorize(s)
    value, admitted = m.binding(s["repo"], b, s["env"], s["event"])
    assert value["tool_commit"] == s["tool"] != value["plan"]["inputs"]["inventory"]["candidate"]
    assert admitted["request_commit"] == s["q"]
    event = copy.deepcopy(s["event"])
    event.update(before=s["q"], after=b)
    event["pull_request"]["head"]["sha"] = b
    assert m.probe_request(s["repo"], s["env"], event, repository=s["repository"]) is None
    (s["repo"] / "ordinary.txt").write_text("fictional code")
    s["commit"]("ordinary one")
    (s["repo"] / "ordinary.txt").write_text("fictional next code")
    latest = s["commit"]("ordinary two")
    event.update(before=b, after=latest)
    event["pull_request"]["head"]["sha"] = latest
    assert m.probe_request(s["repo"], s["env"], event, repository=s["repository"]) is None
    (s["repo"] / m.ingress.REQUEST_PATH).write_bytes(
        (s["repo"] / m.ingress.REQUEST_PATH).read_bytes() + b" "
    )
    (s["repo"] / "ordinary.txt").write_text("fictional mixed")
    mixed = s["commit"]("mixed request")
    event.update(before=latest, after=mixed)
    event["pull_request"]["head"]["sha"] = mixed
    with pytest.raises(ValueError, match="mixed_request_commit"):
        m.probe_request(s["repo"], s["env"], event, repository=s["repository"])


def test_fixed_b_objects_transfer_to_t_checkout_without_branch_read(
    module, git_case, monkeypatch, tmp_path
):
    m, s = module, git_case
    b = ingress_tests.authorize(s)
    # The control job fetched B without checking it out; reproduce that state.
    s["git"]("checkout", "--detach", s["q"])
    execution = tmp_path / "execution"
    execution.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=execution).decode().strip()

    git("init", "-q")
    git("config", "core.autocrlf", "false")
    git("config", "core.hooksPath", "disabled-fictional-hooks")
    git("fetch", "--no-tags", str(s["repo"]), s["tool"])
    git("checkout", "--detach", s["tool"])
    env = dict(s["env"], GITHUB_WORKSPACE=str(s["repo"]), RUNNER_TEMP=str(tmp_path))
    monkeypatch.setattr(m.platform, "system", lambda: "Linux")
    monkeypatch.setattr(m, "tool_snapshot", lambda *a: None)
    monkeypatch.setattr(m, "public_identity", lambda *a: {})
    created = []

    def source_prepare(root, directory, *, mode, candidate, tool_commit):
        assert git("rev-parse", "HEAD") == tool_commit == s["tool"]
        assert candidate == s["candidate"] != tool_commit
        directory.mkdir()
        created.append(mode)

    monkeypatch.setattr(m.versions, "prepare", source_prepare)
    monkeypatch.setattr(
        m.ingress,
        "accept_ci",
        lambda *a, **kw: (s["frozen"], dict(run_id=123, jobs=m.ingress.JOBS)),
    )
    monkeypatch.setattr(
        m.runner,
        "checked",
        lambda frozen, identifier, *a, **kw: (dict(execution_id=identifier), {}),
    )
    registered = []
    monkeypatch.setattr(m.mechanism, "register_plan", lambda *a: registered.append(a))

    def forbidden(*a, **kw):
        raise AssertionError("formal execution reached during preparation")

    monkeypatch.setattr(m.runner, "launch_one", forbidden)
    assert m.prepare(execution, b, env, s["event"], repository=s["repository"]) == dict(
        ready="true"
    )
    assert created == ["candidate", "legacy", "original"] and len(registered) == 1
    assert git("rev-parse", "HEAD") == s["tool"]
    for commit in (b, s["q"], s["p"]):
        git("cat-file", "-e", commit + "^{commit}")


@pytest.mark.parametrize("damage", [None, "tool_head", "source_bytes"])
def test_real_c_not_t_archive_binding_before_fake_execution_and_seal(
    module, monkeypatch, tmp_path, damage
):
    m = module
    root = tmp_path / "repository"
    root.mkdir()

    def git(*args):
        return subprocess.check_output(["git", *args], cwd=root).decode().strip()

    git("init", "-q")
    for key, value in (
        ("user.name", "fictional"),
        ("user.email", "fictional@example.com"),
        ("commit.gpgsign", "false"),
        ("core.autocrlf", "false"),
        ("core.hooksPath", "disabled-fictional-hooks"),
    ):
        git("config", key, value)
    product = root / "src/clytie/__init__.py"
    product.parent.mkdir(parents=True)
    product.write_text("# fictional product only\n")

    def commit(message):
        git("add", ".")
        git("commit", "-qm", message)
        return git("rev-parse", "HEAD")

    c = commit("product C")
    names = tuple(
        sorted(
            m.REQUIRED_TOOLS
            | {
                "scripts/measurement_fixture.py",
                "scripts/measurement_geometry.py",
                "scripts/measurement_preparation_evidence.py",
                "scripts/measurement_preparation_transport.py",
                "scripts/measurement_mechanism_launch.py",
            }
        )
    )
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(
            Path(m.__file__).read_bytes() if name == m.SELF_PATH else b"fictional tool bytes\n"
        )
    t = commit("tool T")
    monkeypatch.setattr(m.launch, "TOOL_FILES", names)
    monkeypatch.setattr(m.runner, "ROOT", root)
    monkeypatch.setattr(m.platform, "system", lambda: "Windows")
    runtime = dict(
        environment=dict(
            os="Windows", cpu_count=2, release_sha256="a" * 64, machine_sha256="b" * 64
        ),
        python="3.10.0",
        sqlite="3.40.0",
    )
    monkeypatch.setattr(m.runner.preparation, "runtime", lambda: runtime)
    env = dict(
        RUNNER_TEMP=str(tmp_path),
        GITHUB_ACTIONS="true",
        GITHUB_REPOSITORY=m.transport.REPOSITORY,
        GITHUB_RUN_ID="123",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_JOB="mechanism_windows",
    )
    target = m.data_root(env)
    (target / "sources").mkdir(parents=True)
    directory = m.sources(target)["candidate"]
    source = m.versions.prepare(root, directory, mode="candidate", candidate=c, tool_commit=t)
    tools = m.launch.tool_inventory(root)
    inventory, geometry = inputs()
    inventory.update(
        candidate=c,
        tool_commit=t,
        source_inventory_sha256=plan.digest(source),
        tool_sha256=tools["sha256"],
        fixture_sha256=tools["files"]["scripts/measurement_fixture.py"],
        environments={name: plan.digest(runtime) for name in plan.PLATFORMS},
    )
    for item in geometry.values():
        item["fixture_sha256"] = inventory["fixture_sha256"]
    frozen = plan.freeze_plan(
        plan.draft_plan("mechanism", inventory, geometry, profiles=profiles())
    )
    rows = m.selected_rows(frozen, env)
    expected = [
        m.runner.checked(frozen, row["execution_id"], directory, environ=env)[0] for row in rows
    ]
    assert c != t and git("rev-parse", "HEAD") == t
    (target / "frozen-plan.json").write_bytes(m.launch.canonical(frozen))
    (target / "ready.json").write_bytes(
        m.launch.canonical(
            dict(
                format=1,
                admission_commit="c" * 40,
                plan_sha256=frozen["plan_sha256"],
                manifests=[plan.digest(item) for item in expected],
            )
        )
    )
    monkeypatch.setattr(m.ingress, "accept_ci", lambda *a, **kw: (frozen, {}))
    monkeypatch.setattr(m, "public_identity", lambda *a: dict(recipient="fictional_recipient"))
    calls = []

    def execute(frozen, execution_id, directory, trial, *, environ):
        calls.append("execute")
        trial.mkdir()
        (trial / "process-state.json").write_text(
            json.dumps(
                dict(
                    format=1,
                    execution_id=execution_id,
                    state="driver_exited",
                    driver_exit_confirmed=True,
                    exit_code=0,
                )
            )
        )
        raw = trial / "trial/raw"
        raw.mkdir(parents=True)
        (raw / "run-manifest.json").write_text(json.dumps(dict(expected[0], result="pass")))
        return dict(exit_code=0)

    def seal(raw, sealed, recipient, expected_manifest):
        calls.append("seal")
        assert expected_manifest["candidate"] == expected_manifest["source_commit"] == c != t
        sealed.mkdir()
        ciphertext = b"fictional ciphertext, encryption deliberately mocked"
        (sealed / "measurements.age").write_bytes(ciphertext)
        (sealed / "receipt.json").write_text(
            json.dumps(
                dict(
                    state="encrypted_pending_verification",
                    format=1,
                    recipient_sha256="a" * 64,
                    ciphertext_sha256=m.ingress.sha(ciphertext),
                    ciphertext_bytes=len(ciphertext),
                    file="measurements.age",
                )
            )
        )

    def repository():
        return dict(full_name=m.transport.REPOSITORY, private=True)

    if damage == "tool_head":
        (root / "new-code.txt").write_text("fictional change")
        commit("unexpected tool head")
    elif damage == "source_bytes":
        (directory / "source/src/clytie/__init__.py").write_text("changed fictional source")
    if damage:
        with pytest.raises(ValueError):
            m.run_slot(root, 0, env, {}, repository=repository, execute=execute, seal=seal)
        assert not calls
    else:
        result = m.run_slot(root, 0, env, {}, repository=repository, execute=execute, seal=seal)
        assert result["succeeded"] and calls == ["execute", "seal"]
        assert m.transport.validate_sealed(target / "slot-0-sealed") == result["paths"]


def test_workflow_permissions_order_and_exact_cipher_uploads():
    workflow = yaml.safe_load(
        (Path(__file__).resolve().parents[1] / ".github/workflows/measurement.yml").read_text()
    )
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["on"] == {
        "pull_request": {"types": ["synchronize"], "paths": [".github/a43-mechanism-request.json"]}
    }
    assert set(workflow["jobs"]) == {"mechanism_windows", "mechanism_linux"}
    assert workflow["jobs"]["mechanism_linux"]["needs"] == "mechanism_windows"
    for job in workflow["jobs"].values():
        slots = [step for step in job["steps"] if step.get("id", "").startswith("slot")]
        assert [step["run"].split()[-1] for step in slots] == [str(index) for index in range(4)]
        assert all("continue-on-error" not in step for step in job["steps"])
        uploads = [
            step
            for step in job["steps"]
            if step.get("uses", "").startswith("actions/upload-artifact@")
        ]
        assert len(uploads) == 4
        for index, upload in enumerate(uploads):
            assert "always()" in upload["if"]
            assert upload["with"]["path"].splitlines() == [
                f"${{{{ steps.slot{index}.outputs.ciphertext }}}}",
                f"${{{{ steps.slot{index}.outputs.receipt }}}}",
            ]
            assert upload["with"]["retention-days"] == 14
