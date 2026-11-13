"""Narrow Q/B wait and four frozen per-platform slots; no retries or new budget."""

import argparse
import asyncio
import base64
import os
import platform
import stat
import subprocess
from pathlib import Path

from scripts import measurement_evidence as evidence
from scripts import measurement_launch as launch
from scripts import measurement_mechanism_launch as runner
from scripts import measurement_mechanism_plan as mechanism
from scripts import measurement_plan as plan
from scripts import measurement_plan_ingress as ingress
from scripts import measurement_preparation_evidence as prep
from scripts import measurement_preparation_transport as preparation
from scripts import measurement_transport as transport
from scripts import measurement_version as versions

ROOT = transport.ROOT
Error = evidence.EvidenceError
BRANCH = "m4-3"
PUBLIC_PATH = ".github/a43-preparation-public.json"
SELF_PATH = "scripts/measurement_mechanism_workflow.py"
REQUIRED_TOOLS = {
    SELF_PATH,
    ingress.TOOL_PATH,
    ingress.WORKFLOW_PATH,
    PUBLIC_PATH,
    "src/clytie/clock.py",
}
POLLS = 20
POLL_SECONDS = 10
NETWORK_SECONDS = 15


def require(condition, code):
    if not condition:
        raise Error(code)


def deny_network(*args, **kwargs):
    raise Error("network_executor_not_supplied")


def control_sleep(seconds):
    # Control-only process. The measurement driver is a separate fresh process;
    # this clock never becomes part of the tested product C's import tree.
    from clytie.clock import SystemClock

    asyncio.run(SystemClock().sleep(seconds))


def live_repository(*, execute=deny_network):
    result = execute(
        ["gh", "api", f"repos/{transport.REPOSITORY}"],
        capture_output=True,
        check=False,
        timeout=NETWORK_SECONDS,
    )
    require(result.returncode == 0, "repository_metadata_unavailable")
    value = evidence._json(result.stdout)
    return dict(full_name=value.get("full_name"), private=value.get("private"))


def fetch_admission(root, env, *, execute=deny_network):
    """One bounded read fetch. Token stays in child environment, never argv/config."""
    token = env.get("GH_TOKEN")
    require(type(token) is str and bool(token) and "\n" not in token, "github_token_missing")
    user_and_secret = ":".join(("x-access-token", token)).encode()
    header = ": ".join(("Authorization", "Basic " + base64.b64encode(user_and_secret).decode()))
    git_env = dict(
        runner._child_environment(env),
        GIT_TERMINAL_PROMPT="0",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_COUNT="2",
        GIT_CONFIG_KEY_0="credential.helper",
        GIT_CONFIG_VALUE_0="",
        GIT_CONFIG_KEY_1="http.extraHeader",
        GIT_CONFIG_VALUE_1=header,
    )
    result = execute(
        [
            "git",
            "fetch",
            "--no-tags",
            "--no-write-fetch-head",
            "https://github.com/" + transport.REPOSITORY + ".git",
            "refs/heads/" + BRANCH + ":refs/measurement/admission-candidate",
        ],
        cwd=root,
        env=git_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=NETWORK_SECONDS,
    )
    require(result.returncode == 0, "admission_fetch_failed")
    commit = (
        versions._git(root, "rev-parse", "refs/measurement/admission-candidate").decode().strip()
    )
    ingress._hash(commit, 40)
    return commit


def probe_request(root, env, event, *, repository=deny_network):
    preparation._private(repository)
    require(
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_REPOSITORY") == transport.REPOSITORY
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and env.get("GITHUB_JOB") in ingress.JOBS.values(),
        "workflow_context_invalid",
    )
    evidence._pattern(env.get("GITHUB_RUN_ID"), r"[1-9][0-9]{0,19}")
    head = versions._git(root, "rev-parse", "HEAD").decode().strip()
    ingress._hash(head, 40)
    parents = versions._git(root, "rev-list", "--parents", "-n", "1", head).decode().split()
    require(len(parents) == 2 and parents[0] == head, "request_single_parent_required")
    ingress._hash(event.get("before"), 40)
    ingress.check_ci_event(event, env, head, event["before"])
    require(event["pull_request"]["head"].get("ref") == BRANCH, "request_branch_invalid")
    require(
        not versions._git(root, "diff", "HEAD", "--name-only", "--").strip(),
        "request_checkout_modified",
    )
    changed = versions._git(
        root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", parents[1], head, "--"
    )
    if changed != ingress.REQUEST_PATH.encode() + b"\0":
        require(ingress.REQUEST_PATH.encode() not in changed.split(b"\0"), "mixed_request_commit")
        require(
            not versions._git(
                root, "diff", "--name-only", "-z", event["before"], head, "--", ingress.REQUEST_PATH
            ),
            "request_changed_in_batch",
        )
        return None
    request = ingress.request_at(root, head)
    require(request["plan_commit"] == parents[1] == event["before"], "request_parent_mismatch")
    return head, request


def tool_snapshot(root, value):
    tools = launch.tool_inventory(root)
    require(REQUIRED_TOOLS <= set(tools["files"]), "workflow_tools_not_inventoried")
    require(
        tools["sha256"] == value["plan"]["inputs"]["inventory"]["tool_sha256"],
        "workflow_tools_changed",
    )
    for path, expected in tools["files"].items():
        require(
            ingress.sha(versions._git(root, "show", value["tool_commit"] + ":" + path)) == expected,
            "workflow_tool_commit_mismatch",
        )
    require(
        ingress.sha(prep._read_file(Path(__file__), ingress.LIMIT)) == tools["files"][SELF_PATH],
        "workflow_running_tool_mismatch",
    )


def binding(root, commit, env, event):
    """The same admission checks before source extraction, without material claims."""
    raw = ingress.git_file(root, commit, ingress.ADMISSION_PATH)
    admission = ingress.unpack(raw)
    evidence._fields(
        admission,
        (
            "format",
            "purpose",
            "campaign",
            "plan_commit",
            "plan_file_sha256",
            "record_sha256",
            "product_commit",
            "tool_commit",
            "request_commit",
            "run_id",
            "attempt",
            "jobs",
        ),
    )
    q = admission["request_commit"]
    ingress._hash(q, 40)
    require(
        ingress.only_change(root, commit, ingress.ADMISSION_PATH) == q, "admission_parent_mismatch"
    )
    request = ingress.request_at(root, q)
    expected = dict(
        request,
        purpose="fixed_run_admission",
        request_commit=q,
        run_id=admission["run_id"],
        attempt=1,
        jobs=ingress.JOBS,
    )
    require(plan.digest(admission) == plan.digest(expected), "admission_mismatch")
    evidence._number(admission["run_id"], integer=True, positive=True)
    ingress.check_ci_event(event, env, q, request["plan_commit"])
    require(
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_REPOSITORY") == transport.REPOSITORY
        and env.get("GITHUB_RUN_ID") == str(admission["run_id"])
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and env.get("GITHUB_JOB") in ingress.JOBS.values(),
        "admission_run_mismatch",
    )
    value, payload, record = ingress.pair(root, request["plan_commit"])
    require(
        ingress.sha(payload) == request["plan_file_sha256"]
        and ingress.sha(record) == request["record_sha256"]
        and value["tool_commit"] == request["tool_commit"]
        and value["plan"]["inputs"]["inventory"]["candidate"] == request["product_commit"],
        "admission_plan_mismatch",
    )
    ingress.public_scan(raw)
    return value, admission


def wait_for_admission(
    root, env, event, *, repository=deny_network, fetch=deny_network, sleep=control_sleep
):
    result = probe_request(root, env, event, repository=repository)
    if result is None:
        return dict(eligible="false")
    q, request = result
    value, _, _ = ingress.pair(root, request["plan_commit"])
    tool_snapshot(root, value)
    for index in range(POLLS):
        commit = fetch(root, env)
        ingress._hash(commit, 40)
        if commit == q:
            if index + 1 < POLLS:
                sleep(POLL_SECONDS)
            continue
        value, admission = binding(root, commit, env, event)
        require(admission["request_commit"] == q, "admission_request_changed")
        preparation._private(repository)
        return dict(
            eligible="true",
            admission_commit=commit,
            request_commit=q,
            tool_commit=value["tool_commit"],
        )
    raise Error("admission_wait_exhausted_no_measurement")


def data_root(env):
    path = Path(env["RUNNER_TEMP"])
    require(path.is_absolute(), "runner_temp_invalid")
    return prep._safe(path) / "a43-mechanism"


def sources(target):
    return {mode: target / "sources" / mode for mode in ("candidate", "legacy", "original")}


def public_identity(root):
    public = evidence._json(prep._read_file(Path(root) / PUBLIC_PATH, 16 * 1024))
    evidence._fields(public, ("format", "recipient", "fingerprint", "tool", "version"))
    require(
        type(public["format"]) is int
        and public["format"] == 1
        and public["tool"] == "pyrage"
        and public["version"] == "1.4.0"
        and public["fingerprint"] == evidence.fingerprint(public["recipient"]),
        "public_identity_invalid",
    )
    return public


def selected_rows(frozen, env):
    system = platform.system()
    require(env.get("GITHUB_JOB") == ingress.JOBS.get(system), "platform_job_mismatch")
    # Preserve the generated plan order, including its platform order. The YAML
    # serial dependency matches this order; a later format cannot silently reorder.
    platforms = list(dict.fromkeys(row["platform"] for row in frozen["runs"]))
    require(platforms == ["Windows", "Linux"], "workflow_platform_order_mismatch")
    rows = [row for row in frozen["runs"] if row["platform"] == system]
    require(len(rows) == 4, "platform_slots_mismatch")
    return rows


def prepare(root, commit, env, event, *, repository=deny_network):
    preparation._private(repository)
    ingress._hash(commit, 40)
    # Q's first checkout fetched B. Transfer that exact local Git object and its
    # ancestry to the separate T checkout; never discover a branch again here.
    control = prep._safe(Path(env["GITHUB_WORKSPACE"]))
    q = event["pull_request"]["head"]["sha"]
    ingress._hash(q, 40)
    require(
        versions._git(control, "rev-parse", "HEAD").decode().strip() == q,
        "control_checkout_changed",
    )
    versions._git(root, "fetch", "--no-tags", "--no-write-fetch-head", str(control), commit)
    value, _ = binding(root, commit, env, event)
    require(
        versions._git(root, "rev-parse", "HEAD").decode().strip() == value["tool_commit"],
        "tool_checkout_mismatch",
    )
    tool_snapshot(root, value)
    public_identity(root)
    target = prep._new(data_root(env))
    (target / "sources").mkdir()
    directories = sources(target)
    for mode, directory in directories.items():
        versions.prepare(
            root,
            directory,
            mode=mode,
            candidate=value["plan"]["inputs"]["inventory"]["candidate"],
            tool_commit=value["tool_commit"],
        )
    frozen, invocation = ingress.accept_ci(
        root, commit, env, directories, event=event, repository=repository
    )
    manifests = [
        runner.checked(frozen, row["execution_id"], directories["candidate"], environ=env)[0]
        for row in selected_rows(frozen, env)
    ]
    mechanism.register_plan(
        frozen, dict(run_id=invocation["run_id"], attempt=1, jobs=invocation["jobs"])
    )
    prep._save_bytes(target / "frozen-plan.json", launch.canonical(frozen))
    prep._save_bytes(
        target / "ready.json",
        launch.canonical(
            dict(
                format=1,
                admission_commit=commit,
                plan_sha256=frozen["plan_sha256"],
                manifests=[plan.digest(item) for item in manifests],
            )
        ),
    )
    return dict(ready="true")


def confirmed_exit(target, execution_id):
    state = evidence._json(prep._read_file(target / "process-state.json", 4096))
    evidence._fields(
        state, ("format", "execution_id", "state", "driver_exit_confirmed", "exit_code")
    )
    require(
        type(state["format"]) is int
        and state["format"] == 1
        and state["execution_id"] == execution_id
        and state["state"] == "driver_exited"
        and state["driver_exit_confirmed"] is True
        and type(state["exit_code"]) is int,
        "driver_exit_unconfirmed",
    )
    return state["exit_code"]


def outcome(
    execution_id,
    *,
    state,
    failure_stage,
    execute_state,
    driver_exit_confirmed=False,
    driver_exit_code=None,
    ciphertext_saved=False,
    upload_eligible=False,
):
    return dict(
        execution_id=execution_id,
        state=state,
        accepted=False,
        failure_stage=failure_stage,
        execute_state=execute_state,
        driver_exit_confirmed=driver_exit_confirmed,
        driver_exit_code=driver_exit_code,
        worker_exit="not_evaluated_here",
        ciphertext_saved=ciphertext_saved,
        upload_eligible=upload_eligible,
        artifact_upload="not_observed",
    )


def run_slot(
    root,
    ordinal,
    env,
    event,
    *,
    repository=deny_network,
    execute=runner.launch_one,
    seal=evidence.seal,
):
    require(type(ordinal) is int and 0 <= ordinal < 4, "slot_invalid")
    target = data_root(env)
    ready = evidence._json(prep._read_file(target / "ready.json", 16 * 1024))
    evidence._fields(ready, ("format", "admission_commit", "plan_sha256", "manifests"))
    require(type(ready["format"]) is int and ready["format"] == 1, "ready_invalid")
    frozen, _ = ingress.accept_ci(
        root, ready["admission_commit"], env, sources(target), event=event, repository=repository
    )
    stored = evidence._json(prep._read_file(target / "frozen-plan.json", ingress.LIMIT))
    require(
        plan.digest(stored) == plan.digest(frozen)
        and ready["plan_sha256"] == frozen["plan_sha256"],
        "prepared_plan_changed",
    )
    row = selected_rows(frozen, env)[ordinal]
    expected, _ = runner.checked(
        frozen, row["execution_id"], sources(target)["candidate"], environ=env
    )
    require(
        type(ready["manifests"]) is list
        and len(ready["manifests"]) == 4
        and ready["manifests"][ordinal] == plan.digest(expected),
        "prepared_manifest_changed",
    )
    if ordinal:
        previous = evidence._json(
            prep._read_file(target / f"slot-{ordinal - 1}-outcome.json", 4096)
        )
        previous_id = selected_rows(frozen, env)[ordinal - 1]["execution_id"]
        require(
            plan.digest(previous)
            == plan.digest(
                outcome(
                    previous_id,
                    state="driver_success_encrypted_pending_verification",
                    failure_stage=None,
                    execute_state="returned_success",
                    driver_exit_confirmed=True,
                    driver_exit_code=0,
                    ciphertext_saved=True,
                    upload_eligible=True,
                )
            ),
            "earlier_slot_not_complete",
        )
    public = public_identity(root)
    preparation._private(repository)
    prep._save_bytes(
        target / f"slot-{ordinal}-dispatch.json",
        launch.canonical(dict(execution_id=row["execution_id"], state="dispatch_claimed_no_retry")),
    )
    trial = target / f"slot-{ordinal}"
    succeeded = False
    paths = []
    failure_stage = None
    execute_state = "not_started"
    exited, code, saved = False, None, False
    try:
        handoff = execute(
            frozen, row["execution_id"], sources(target)["candidate"], trial, environ=env
        )
        succeeded = handoff["exit_code"] == 0 and type(handoff["exit_code"]) is int
        execute_state = "returned_success" if succeeded else "returned_failure"
        if not succeeded:
            failure_stage = "execute"
    except BaseException:
        # Retain unknown process state. Never kill, retry or assume cancellation
        # means the actual child has exited. Only stable finished raw can be sealed.
        succeeded = False
        failure_stage = "execute"
        execute_state = "raised_or_interrupted"
    stage = "exit_check"
    try:
        code = confirmed_exit(trial, row["execution_id"])
        exited = True
        stage = "material_check"
        after, _ = runner.checked(
            frozen, row["execution_id"], sources(target)["candidate"], environ=env
        )
        require(plan.digest(after) == plan.digest(expected), "post_materials_changed")
        raw = trial / "trial" / "raw"
        stage = "raw_manifest"
        actual = evidence._json(prep._read_file(raw / "run-manifest.json", ingress.LIMIT))
        succeeded = succeeded and code == 0 and actual["result"] == row["expected_result"]
        if not succeeded and failure_stage is None:
            failure_stage = "raw_manifest"
        sealed = target / f"slot-{ordinal}-sealed"
        stage = "seal"
        seal(raw, sealed, public["recipient"], expected)
        saved = True
        stage = "upload_gate"
        paths = transport.upload_gate(sealed, repository=repository)
    except BaseException:
        succeeded = False
        paths = []
        failure_stage = stage
    state = (
        "driver_success_encrypted_pending_verification"
        if succeeded and paths
        else "failed_no_retry"
    )
    observed = outcome(
        row["execution_id"],
        state=state,
        failure_stage=failure_stage,
        execute_state=execute_state,
        driver_exit_confirmed=exited,
        driver_exit_code=code,
        ciphertext_saved=saved,
        upload_eligible=bool(paths),
    )
    prep._save_bytes(target / f"slot-{ordinal}-outcome.json", launch.canonical(observed))
    return dict(
        succeeded=state == "driver_success_encrypted_pending_verification",
        paths=paths,
        outcome=observed,
    )


def output(env, values):
    for key, value in values.items():
        require(
            key
            in {
                "eligible",
                "admission_commit",
                "request_commit",
                "tool_commit",
                "ready",
                "ciphertext",
                "receipt",
            }
            and type(value) is str
            and "\n" not in value
            and "\r" not in value,
            "output_invalid",
        )
    path = prep._safe(Path(env["GITHUB_OUTPUT"]))
    before = path.stat()
    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, "output_not_regular")
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "a", encoding="utf-8", newline="\n") as stream:
        after = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(after.st_mode)
            and after.st_nlink == 1
            and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns),
            "output_changed",
        )
        stream.write("".join(key + "=" + value + "\n" for key, value in values.items()))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("wait")
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--admission-commit", required=True)
    slot_parser = commands.add_parser("run-slot")
    slot_parser.add_argument("--slot", type=int, choices=range(4), required=True)
    args = parser.parse_args(argv)
    env = os.environ
    try:
        require(env.get("GITHUB_ACTIONS") == "true", "ci_only")
        event = ingress.read_ci_event(env)

        def repository():
            return live_repository(execute=subprocess.run)

        if args.command == "wait":
            values = wait_for_admission(
                ROOT,
                env,
                event,
                repository=repository,
                fetch=lambda root, current: fetch_admission(root, current, execute=subprocess.run),
            )
        elif args.command == "prepare":
            values = prepare(ROOT, args.admission_commit, env, event, repository=repository)
        else:
            result = run_slot(ROOT, args.slot, env, event, repository=repository)
            values = dict(zip(("ciphertext", "receipt"), result["paths"]))
            if values:
                output(env, values)
            print(
                "mechanism_slot_pending_local_verification"
                if result["succeeded"]
                else "mechanism_slot_failed_no_retry"
            )
            status = result["outcome"]
            print(
                "mechanism_stage="
                + (status["failure_stage"] or "sealed_pending_local_verification")
                + " driver_exit_confirmed="
                + str(status["driver_exit_confirmed"]).lower()
                + " ciphertext_saved="
                + str(status["ciphertext_saved"]).lower()
                + " artifact_upload=not_observed worker_exit=not_evaluated_here"
            )
            return 0 if result["succeeded"] else 1
        output(env, values)
        print("mechanism_control_only")
        return 0
    except (Exception, KeyboardInterrupt):
        print("mechanism_workflow_rejected_no_retry")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
