"""Narrow Q/B wait and four frozen per-platform slots; no retries or new budget."""

import argparse
import os
import platform
import subprocess
from pathlib import Path

if __package__:
    from . import measurement_cipher_handoff as cipher_handoff
    from . import measurement_evidence as evidence
    from . import measurement_launch as launch
    from . import measurement_matrix_ingress as ingress
    from . import measurement_matrix_launch as runner
    from . import measurement_matrix_plan as matrix
    from . import measurement_mechanism_workflow as common
    from . import measurement_plan as plan
    from . import measurement_preparation_evidence as prep
    from . import measurement_transport as transport
    from . import measurement_version as versions
else:
    import measurement_cipher_handoff as cipher_handoff
    import measurement_evidence as evidence
    import measurement_launch as launch
    import measurement_matrix_ingress as ingress
    import measurement_matrix_launch as runner
    import measurement_matrix_plan as matrix
    import measurement_mechanism_workflow as common
    import measurement_plan as plan
    import measurement_preparation_evidence as prep
    import measurement_transport as transport
    import measurement_version as versions

ROOT = transport.ROOT
Error = evidence.EvidenceError
BRANCH = "m4-3"
PUBLIC_PATH = ".github/a43-preparation-public.json"
SELF_PATH = "scripts/measurement_matrix_workflow.py"
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


require = common.require


deny_network = common.deny_network


control_sleep = common.control_sleep


live_repository = common.live_repository


fetch_admission = common.fetch_admission


def probe_request(root, env, event, *, repository=deny_network):
    transport.measurement_repository(repository)
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
        transport.measurement_repository(repository)
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
    return prep._safe(path) / "a43-matrix"


sources = common.sources


public_identity = common.public_identity


def selected_rows(frozen, env):
    system = platform.system()
    matches = [
        row
        for row in frozen["runs"]
        if row["position"] == 0
        and matrix.jobs(frozen)[matrix.group_key(row)] == env.get("GITHUB_JOB")
    ]
    require(len(matches) == 1 and matches[0]["platform"] == system, "platform_job_mismatch")
    row = matches[0]
    return matrix.group_rows(frozen, system, row["scale"], row["group"])


def prepare(root, commit, env, event, *, repository=deny_network):
    transport.measurement_repository(repository)
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
        runner.checked(frozen, row["execution_id"], target / "sources", environ=env)[0]
        for row in selected_rows(frozen, env)
    ]
    matrix.register_plan(
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


confirmed_exit = common.confirmed_exit


outcome = common.outcome


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
    expected, _ = runner.checked(frozen, row["execution_id"], target / "sources", environ=env)
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
    transport.measurement_repository(repository)
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
        handoff = execute(frozen, row["execution_id"], target / "sources", trial, environ=env)
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
        after, _ = runner.checked(frozen, row["execution_id"], target / "sources", environ=env)
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
        # Public authorization is confined to this fully checked slot path:
        # accept_ci, runner.checked, confirmed_exit, raw identity and seal above.
        # Generic transport upload_gate remains PRIVATE-only.
        transport.measurement_repository(repository)
        paths = transport.validate_sealed(sealed)
        prep._save_bytes(
            trial / "sealed-binding.json", launch.canonical(cipher_handoff.sealed_binding(sealed))
        )
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


output = common.output


def complete_handoff(
    frozen,
    execution_id,
    directory,
    trial,
    sealed,
    env,
    *,
    request_commit,
    artifact_id,
    artifact_digest,
    github=deny_network,
):
    """Write only after a checked successful driver exit and encrypted pair.

    This is a local arm handoff, not an acceptance or upload receipt. YAML
    success dependencies additionally stop after upload failure/unknown result.
    """
    manifest, inputs = runner.checked(frozen, execution_id, directory, environ=env)
    transport.validate_sealed(sealed)
    require(confirmed_exit(trial, execution_id) == 0, "matrix_handoff_exit_failed")
    actual = evidence._json(prep._read_file(trial / "trial/raw/run-manifest.json", ingress.LIMIT))
    row = matrix.select(frozen, execution_id)
    require(actual["result"] == row["expected_result"], "matrix_handoff_result_failed")
    require(
        {k: actual[k] for k in manifest if k not in {"result", "metrics"}}
        == {k: v for k, v in manifest.items() if k not in {"result", "metrics"}},
        "matrix_handoff_manifest_changed",
    )
    require(
        all(
            actual["metrics"][key] == manifest["metrics"][key] for key in ("logical", "ledger_rows")
        ),
        "matrix_handoff_workload_changed",
    )
    cipher_handoff.confirm(
        trial,
        sealed,
        env,
        name=f"a43-encrypted-{env['GITHUB_RUN_ID']}-1-{env['GITHUB_JOB']}-slot{row['position']}",
        workflow_path=ingress.WORKFLOW_PATH,
        request_commit=request_commit,
        artifact_id=artifact_id,
        artifact_digest=artifact_digest,
        github=github,
    )
    registry = matrix._registry()
    expected = dict(
        format=1,
        plan_sha256=frozen["plan_sha256"],
        execution_id=execution_id,
        manifest_sha256=plan.digest(manifest),
        workflow=inputs["workflow"],
    )
    for suffix, state in (("started", "started_no_retry"), ("entered", "child_entered_no_retry")):
        record = evidence._json(
            prep._read_file(registry / (execution_id + "." + suffix + ".json"), 64 * 1024)
        )
        require(record == dict(expected, state=state), "matrix_handoff_claim_mismatch")
    path = registry / (execution_id + ".completed.json")
    matrix.mechanism._safe_new_path(path)
    matrix.mechanism._write_exclusive(
        path, dict(expected, state="driver_success_encrypted_pending_verification")
    )


def confirm_upload(
    root,
    ordinal,
    env,
    event,
    *,
    artifact_id,
    artifact_digest,
    repository=deny_network,
    github=deny_network,
):
    require(type(ordinal) is int and 0 <= ordinal < 4, "slot_invalid")
    target = data_root(env)
    ready = evidence._json(prep._read_file(target / "ready.json", 16 * 1024))
    frozen, invocation = ingress.accept_ci(
        root, ready["admission_commit"], env, sources(target), event=event, repository=repository
    )
    require(ready["plan_sha256"] == frozen["plan_sha256"], "prepared_plan_changed")
    row = selected_rows(frozen, env)[ordinal]
    observed = evidence._json(prep._read_file(target / f"slot-{ordinal}-outcome.json", 4096))
    require(
        observed
        == outcome(
            row["execution_id"],
            state="driver_success_encrypted_pending_verification",
            failure_stage=None,
            execute_state="returned_success",
            driver_exit_confirmed=True,
            driver_exit_code=0,
            ciphertext_saved=True,
            upload_eligible=True,
        ),
        "matrix_upload_slot_not_successful",
    )
    complete_handoff(
        frozen,
        row["execution_id"],
        target / "sources",
        target / f"slot-{ordinal}",
        target / f"slot-{ordinal}-sealed",
        env,
        request_commit=invocation["request_commit"],
        artifact_id=artifact_id,
        artifact_digest=artifact_digest,
        github=github,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("wait")
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--admission-commit", required=True)
    slot_parser = commands.add_parser("run-slot")
    slot_parser.add_argument("--slot", type=int, choices=range(4), required=True)
    confirm = commands.add_parser("confirm-upload")
    confirm.add_argument("--slot", type=int, choices=range(4), required=True)
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
        elif args.command == "confirm-upload":
            evidence._pattern(env.get("ARTIFACT_ID"), r"[1-9][0-9]{0,19}")
            confirm_upload(
                ROOT,
                args.slot,
                env,
                event,
                artifact_id=int(env["ARTIFACT_ID"]),
                artifact_digest=env["ARTIFACT_DIGEST"],
                repository=repository,
                github=transport._github,
            )
            print("matrix_upload_confirmed_pending_local_verification")
            return 0
        else:
            result = run_slot(ROOT, args.slot, env, event, repository=repository)
            values = dict(zip(("ciphertext", "receipt"), result["paths"]))
            if values:
                output(env, values)
            print(
                "matrix_slot_pending_local_verification"
                if result["succeeded"]
                else "matrix_slot_failed_no_retry"
            )
            status = result["outcome"]
            print(
                "matrix_stage="
                + (status["failure_stage"] or "sealed_pending_local_verification")
                + " driver_exit_confirmed="
                + str(status["driver_exit_confirmed"]).lower()
                + " ciphertext_saved="
                + str(status["ciphertext_saved"]).lower()
                + " artifact_upload=not_observed worker_exit=not_evaluated_here"
            )
            return 0 if result["succeeded"] else 1
        output(env, values)
        print("matrix_control_only")
        return 0
    except (Exception, KeyboardInterrupt):
        print("matrix_workflow_rejected_no_retry")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
