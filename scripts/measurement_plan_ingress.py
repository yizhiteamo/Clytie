"""Fixed-file plan ingress. No trial, decryption, upload, or execution loop.

Private scan provenance is a local operational trust boundary, not a signature.
The outer operator publishes only returned bytes after the required scans.
"""

import hashlib
import io
import json
import math
import stat
import tarfile
from decimal import Decimal
from pathlib import Path

from scripts import measurement_evidence as evidence
from scripts import measurement_launch as launch
from scripts import measurement_mechanism_plan as mechanism
from scripts import measurement_plan as plan
from scripts import measurement_preparation_evidence as prep
from scripts import measurement_preparation_request as request_gate
from scripts import measurement_preparation_transport as preparation
from scripts import measurement_transport as base
from scripts import measurement_version as versions

Error = evidence.EvidenceError
PURPOSE = "non_sensitive_mechanism_plan"
CAMPAIGN = "a43_issue72_mechanism_round_one"
CAMPAIGN_ROOT = base.ROOT / ".cache" / "measurement-plan-ingress-campaign"
PLAN_PATH = ".github/a43-mechanism-plan.json"
RECORD_PATH = ".github/a43-mechanism-verification.json"
REQUEST_PATH = ".github/a43-mechanism-request.json"
ADMISSION_PATH = ".github/a43-mechanism-admission.json"
WORKFLOW_PATH = ".github/workflows/measurement.yml"
PULL_REQUEST = 71
TOOL_PATH = "scripts/measurement_plan_ingress.py"
JOBS = {"Linux": "mechanism_linux", "Windows": "mechanism_windows"}
THRESHOLDS = {
    "control_p99_ms": 100,
    "control_max_ms": 250,
    "no_backup_p99_ratio": 1.2,
    "no_backup_p99_add_ms": 20,
    "backup_p99_ratio": 2,
    "backup_p99_add_ms": 100,
    "capture": "unchanged_wait_and_hold_limits",
    "resources": "no_lost_completion_duplicate_write_or_persistent_growth",
}
LIMIT = 512 * 1024
CHECKS = [
    "frozen_plan_rebuilt",
    "four_legal_scales",
    "fixed_old_references",
    "eight_mechanism_slots",
    "fixed_parameters_and_evidence",
    "actual_source_and_tool_bytes",
    "local_basis_bytes",
]


def require(condition, code):
    if not condition:
        raise Error(code)


def sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _encode(value):
    if type(value) in {int, float}:
        require(math.isfinite(value) and 0 <= value <= 2**53, "numeric_limit")
        if type(value) is int:
            return {"integer": format(value, ",")}
        whole, fraction = format(Decimal(str(value)), ",f").split(".")
        fraction = " ".join(fraction[i : i + 3] for i in range(0, len(fraction), 3))
        return {"real": whole + "." + fraction}
    if type(value) is dict:
        return {key: _encode(item) for key, item in value.items()}
    if type(value) is list:
        return [_encode(item) for item in value]
    return value


def pack(value):
    return json.dumps(
        _encode(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _decode(value):
    if type(value) is dict:
        if set(value) in ({"integer"}, {"real"}):
            kind = next(iter(value))
            text = value[kind]
            integer = r"(?:0|[1-9][0-9]{0,2}(?:,[0-9]{3})*)"
            evidence._pattern(
                text, integer + (r"\.[0-9]{1,3}(?: [0-9]{1,3})*" if kind == "real" else "")
            )
            number = (
                int(text.replace(",", ""))
                if kind == "integer"
                else float(text.replace(",", "").replace(" ", ""))
            )
            require(_encode(number) == value, "noncanonical_decimal")
            return number
        return {key: _decode(item) for key, item in value.items()}
    if type(value) is list:
        return [_decode(item) for item in value]
    require(type(value) not in {int, float}, "unwrapped_number")
    return value


def unpack(raw):
    require(type(raw) is bytes and len(raw) <= LIMIT, "input_size_limit")
    value = _decode(evidence._json(raw))
    require(pack(value) == raw, "noncanonical_file")
    return value


def _hash(value, length=64):
    evidence._pattern(value, "[a-f0-9]{%d}" % length)


def basis_hashes(frozen):
    result = {frozen["inputs"]["inventory"]["verification_sha256"]}
    for profiles in frozen["inputs"]["profiles"].values():
        for profile in profiles.values():
            result.add(profile["basis"])
            require(len(profile["phases"]) <= 16, "phase_count_limit")
            for number, phase in enumerate(profile["phases"], 1):
                require(phase["name"] == f"phase_{number:02d}", "phase_name_invalid")
                result.add(phase["basis"])
            for reserve in profile["reserves"].values():
                result.add(reserve["basis"])
    for value in result:
        _hash(value)
    return result


def validate_payload(raw):
    value = unpack(raw)
    evidence._fields(value, ("format", "purpose", "tool_commit", "thresholds", "plan"))
    require(
        type(value["format"]) is int and value["format"] == 1 and value["purpose"] == PURPOSE,
        "payload_format",
    )
    _hash(value["tool_commit"], 40)
    require(plan.digest(value["thresholds"]) == plan.digest(THRESHOLDS), "threshold_changed")
    frozen = value["plan"]
    plan.validate_plan(frozen)
    require(
        frozen["inputs"]["inventory"].get("tool_commit") == value["tool_commit"],
        "explicit_tool_commit_required",
    )
    require(
        frozen["inputs"]["kind"] == "mechanism" and len(frozen["runs"]) == 8, "mechanism_scope_only"
    )
    require("source_inventory_sha256" in frozen["inputs"]["inventory"], "source_binding_missing")
    # The pure planner only needs the two selected mechanism profiles. Ingress
    # must validate the other supplied scale profiles too, so they cannot carry
    # unknown fields or arbitrary strings that happen not to affect the runs.
    for platform, profiles in frozen["inputs"]["profiles"].items():
        for scale, profile in profiles.items():
            plan.derive_window(frozen["inputs"]["geometry"][platform]["scales"][scale], profile)
    basis_hashes(frozen)
    return value


def public_scan(raw):
    scanner = evidence.public_scanner()
    for line in raw.decode("utf-8").splitlines():
        hits = scanner(line)
        require(type(hits) is set and not hits, "public_scan_failed")


def dual_scan(paths):
    before = [prep._read_file(path, LIMIT) for path in paths]
    for raw in before:
        public_scan(raw)
    require(evidence.private_scan(paths) == "pass", "private_scan_not_pass")
    require(before == [prep._read_file(path, LIMIT) for path in paths], "scan_input_changed")


def git_file(root, commit, path):
    _hash(commit, 40)
    require(path in {PLAN_PATH, RECORD_PATH, REQUEST_PATH, ADMISSION_PATH}, "git_path_not_allowed")
    tree = versions._git(root, "ls-tree", "-z", commit, "--", path)
    require(
        tree.startswith(format(stat.S_IFREG | 0o644, "o").encode() + b" blob ")
        and tree.endswith(b"\t" + path.encode() + b"\0")
        and tree.count(b"\0") == 1,
        "git_blob_required",
    )
    object_id = tree.split(b" ", 2)[2].split(b"\t")[0].decode()
    require(int(versions._git(root, "cat-file", "-s", object_id)) <= LIMIT, "git_blob_size_limit")
    raw = versions._git(root, "cat-file", "blob", object_id)
    unpack(raw)
    return raw


def only_change(root, commit, path):
    _hash(commit, 40)
    parents = versions._git(root, "rev-list", "--parents", "-n", "1", commit).decode().split()
    require(len(parents) == 2 and parents[0] == commit, "single_parent_required")
    changed = versions._git(
        root, "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", parents[1], commit, "--"
    )
    require(changed == path.encode() + b"\0", "single_control_file_required")
    return parents[1]


def materials(root, value, source_directories):
    """Actual archive/tool inventories; never trust an input's self-reported hash."""
    require(
        versions._git(root, "rev-parse", "HEAD").decode().strip() == value["tool_commit"],
        "tool_checkout_mismatch",
    )
    tools = launch.tool_inventory(root)
    require(
        TOOL_PATH in tools["files"] and WORKFLOW_PATH in tools["files"],
        "ingress_not_in_tool_inventory",
    )
    require(
        sha(prep._read_file(Path(__file__), LIMIT)) == tools["files"][TOOL_PATH],
        "running_ingress_tool_mismatch",
    )
    for path, expected in tools["files"].items():
        require(
            sha(versions._git(root, "show", value["tool_commit"] + ":" + path)) == expected,
            "tool_commit_bytes_mismatch",
        )
    frozen = value["plan"]
    candidate = frozen["inputs"]["inventory"]["candidate"]
    evidence._fields(source_directories, ("candidate", "legacy", "original"))
    source = versions.verify(source_directories["candidate"])
    mechanism._check_materials(frozen, source, tools)
    preparation.material_identity(root, source_directories["candidate"], candidate)
    for mode, commit in (("legacy", versions.LEGACY), ("original", versions.ORIGINAL)):
        inventory = versions.verify(source_directories[mode])
        evidence._fields(
            inventory, ("format", "mode", "candidate", "source_commit", "archive_sha256", "files")
        )
        require(
            inventory["format"] == 1
            and type(inventory["format"]) is int
            and inventory["mode"] == mode
            and inventory["candidate"] == candidate
            and inventory["source_commit"] == commit,
            "old_source_mismatch",
        )
        archive = versions._git(root, "archive", "--format=tar", commit)
        files = {}
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            for member in bundle:
                if member.isdir():
                    continue
                require(member.isfile() and member.name not in files, "old_archive_invalid")
                files[member.name] = sha(bundle.extractfile(member).read())
        require(
            inventory["archive_sha256"] == sha(archive) and inventory["files"] == files,
            "old_archive_mismatch",
        )


def _record(value, raw):
    inv = value["plan"]["inputs"]["inventory"]
    return dict(
        format=1,
        purpose="local_plan_verification",
        campaign=CAMPAIGN,
        plan_file_sha256=sha(raw),
        plan_sha256=value["plan"]["plan_sha256"],
        product_commit=inv["candidate"],
        tool_commit=value["tool_commit"],
        tool_sha256=inv["tool_sha256"],
        verification_input_sha256=inv["verification_sha256"],
        checks=CHECKS,
        public_scan="pass",
        private_scan="pass_local_only",
    )


def record_local(source, target, root, source_directories, basis_files):
    base._local_only()
    preparation._private(base.repository)
    raw = prep._read_file(source, LIMIT)
    value = validate_payload(raw)
    require(set(basis_files) == basis_hashes(value["plan"]), "basis_set_mismatch")
    for expected, path in basis_files.items():
        require(sha(prep._read_file(path, LIMIT)) == expected, "basis_bytes_mismatch")
    materials(root, value, source_directories)
    target = prep._new(target)
    prep._save_bytes(target / "plan.json", raw)
    prep._save_bytes(target / "verification.json", pack(_record(value, raw)))
    dual_scan([target / "plan.json", target / "verification.json"])
    materials(root, value, source_directories)
    for expected, path in basis_files.items():
        require(sha(prep._read_file(path, LIMIT)) == expected, "basis_bytes_changed")
    # This local success marker is created only after both scans actually return.
    # It is never uploaded and cannot itself establish cryptographic provenance.
    prep._save_bytes(
        target / "verified.json",
        pack(
            dict(
                plan_file_sha256=sha(raw),
                record_sha256=sha(prep._read_file(target / "verification.json", LIMIT)),
            )
        ),
    )
    return target


def pair(root, commit):
    raw = git_file(root, commit, PLAN_PATH)
    record = git_file(root, commit, RECORD_PATH)
    value = validate_payload(raw)
    require(
        plan.digest(unpack(record)) == plan.digest(_record(value, raw)),
        "verification_record_mismatch",
    )
    public_scan(raw)
    public_scan(record)
    return value, raw, record


def local_pair(root, commit, local):
    value, raw, record = pair(root, commit)
    paths = [Path(local) / "plan.json", Path(local) / "verification.json"]
    require(
        [prep._read_file(path, LIMIT) for path in paths] == [raw, record], "committed_bytes_changed"
    )
    require(
        unpack(prep._read_file(Path(local) / "verified.json", LIMIT))
        == dict(plan_file_sha256=sha(raw), record_sha256=sha(record)),
        "local_verification_missing",
    )
    dual_scan(paths)
    return value, raw, record


def request_bytes(root, plan_commit, local):
    base._local_only()
    preparation._private(base.repository)
    value, raw, record = local_pair(root, plan_commit, local)
    request = dict(
        format=1,
        purpose="wait_for_local_admission_only",
        campaign=CAMPAIGN,
        plan_commit=plan_commit,
        plan_file_sha256=sha(raw),
        record_sha256=sha(record),
        product_commit=value["plan"]["inputs"]["inventory"]["candidate"],
        tool_commit=value["tool_commit"],
    )
    path = Path(local) / "request.json"
    prep._save_bytes(path, pack(request))
    dual_scan([path])
    return prep._read_file(path, LIMIT)


def request_at(root, commit):
    value = unpack(git_file(root, commit, REQUEST_PATH))
    evidence._fields(
        value,
        (
            "format",
            "purpose",
            "campaign",
            "plan_commit",
            "plan_file_sha256",
            "record_sha256",
            "product_commit",
            "tool_commit",
        ),
    )
    require(
        type(value["format"]) is int
        and value["format"] == 1
        and value["purpose"] == "wait_for_local_admission_only"
        and value["campaign"] == CAMPAIGN,
        "request_format",
    )
    for key in ("plan_commit", "product_commit", "tool_commit"):
        _hash(value[key], 40)
    for key in ("plan_file_sha256", "record_sha256"):
        _hash(value[key])
    require(
        only_change(root, commit, REQUEST_PATH) == value["plan_commit"], "request_parent_mismatch"
    )
    public_scan(git_file(root, commit, REQUEST_PATH))
    return value


def authorize_run(
    root, request_commit, local, run_id, *, repository=base.repository, github=base._github
):
    """Local-only: choose an observed Actions run, reserve campaign before return."""
    base._local_only()
    preparation._private(repository)
    evidence._number(run_id, integer=True, positive=True)
    request = request_at(root, request_commit)
    value, raw, record = local_pair(root, request["plan_commit"], local)
    require(
        request["plan_file_sha256"] == sha(raw)
        and request["record_sha256"] == sha(record)
        and request["product_commit"] == value["plan"]["inputs"]["inventory"]["candidate"]
        and request["tool_commit"] == value["tool_commit"],
        "request_binding_mismatch",
    )
    run = github(f"repos/{base.REPOSITORY}/actions/runs/{run_id}")
    require(
        type(run) is dict
        and type(run.get("id")) is int
        and run["id"] == run_id
        and type(run.get("run_attempt")) is int
        and run["run_attempt"] == 1
        and run.get("head_sha") == request_commit
        and run.get("event") == "pull_request"
        and run.get("path") == WORKFLOW_PATH
        and run.get("status") in {"queued", "in_progress"}
        and run.get("repository", {}).get("full_name") == base.REPOSITORY
        and run.get("head_repository", {}).get("full_name") == base.REPOSITORY,
        "observed_run_mismatch",
    )
    admission = dict(
        request,
        purpose="fixed_run_admission",
        request_commit=request_commit,
        run_id=run_id,
        attempt=1,
        jobs=JOBS,
    )
    # One fixed approved campaign, independent of plan, request, run and output.
    # Partial writes/failures remain blocked; do not delete/retry/reset this root.
    mechanism._safe_new_path(CAMPAIGN_ROOT)
    CAMPAIGN_ROOT.mkdir(parents=True, exist_ok=True)
    path = CAMPAIGN_ROOT / "admission.json"
    prep._save_bytes(path, pack(admission))
    dual_scan([path])
    return prep._read_file(path, LIMIT)


def read_ci_event(env):
    """Read only the Actions-provided event filename; never accept a CLI filename."""
    require(env.get("GITHUB_ACTIONS") == "true", "ci_context_required")
    return evidence._json(prep._read_file(Path(env["GITHUB_EVENT_PATH"]), 2 * 1024**2))


def check_ci_event(event, env, request_commit, plan_commit):
    try:
        require(env.get("GITHUB_EVENT_NAME") == "pull_request", "ci_event_mismatch")
        require(
            env.get("GITHUB_WORKFLOW_REF")
            == base.REPOSITORY + "/" + WORKFLOW_PATH + f"@refs/pull/{PULL_REQUEST}/merge",
            "ci_workflow_mismatch",
        )
        identifier = event["repository"]["id"]
        evidence._number(identifier, integer=True, positive=True)
        require(env.get("GITHUB_REPOSITORY_ID") == str(identifier), "ci_repository_id_mismatch")
        pr = event["pull_request"]
        for repo in (event["repository"], pr["head"]["repo"], pr["base"]["repo"]):
            request_gate._repository(repo, identifier)
        require(
            event.get("action") == "synchronize"
            and type(event.get("number")) is int
            and event["number"] == PULL_REQUEST
            and type(pr.get("number")) is int
            and pr["number"] == PULL_REQUEST
            and pr.get("state") == "open"
            and pr["base"]["ref"] == "main"
            and event.get("before") == plan_commit
            and event.get("after") == request_commit
            and pr["head"]["sha"] == request_commit,
            "ci_request_event_mismatch",
        )
    except (KeyError, TypeError):
        raise Error("ci_event_incomplete") from None


def accept_ci(
    root, admission_commit, env, source_directories, *, event, repository=base.repository
):
    """One immutable admitted run; caller separately owns durable start markers."""
    preparation._private(repository)
    admission_raw = git_file(root, admission_commit, ADMISSION_PATH)
    admission = unpack(admission_raw)
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
    require(admission["purpose"] == "fixed_run_admission", "admission_purpose")
    _hash(admission["request_commit"], 40)
    require(
        only_change(root, admission_commit, ADMISSION_PATH) == admission["request_commit"],
        "admission_parent_mismatch",
    )
    request = request_at(root, admission["request_commit"])
    check_ci_event(event, env, admission["request_commit"], request["plan_commit"])
    expected = dict(
        request,
        purpose="fixed_run_admission",
        request_commit=admission["request_commit"],
        run_id=admission["run_id"],
        attempt=1,
        jobs=JOBS,
    )
    require(plan.digest(expected) == plan.digest(admission), "admission_binding_mismatch")
    evidence._number(admission["run_id"], integer=True, positive=True)
    require(
        env.get("GITHUB_ACTIONS") == "true"
        and env.get("GITHUB_REPOSITORY") == base.REPOSITORY
        and env.get("GITHUB_RUN_ID") == str(admission["run_id"])
        and env.get("GITHUB_RUN_ATTEMPT") == "1"
        and env.get("GITHUB_JOB") in JOBS.values(),
        "ci_invocation_mismatch",
    )
    value, raw, record = pair(root, request["plan_commit"])
    require(
        sha(raw) == request["plan_file_sha256"]
        and sha(record) == request["record_sha256"]
        and value["tool_commit"] == request["tool_commit"]
        and value["plan"]["inputs"]["inventory"]["candidate"] == request["product_commit"],
        "committed_binding_mismatch",
    )
    public_scan(admission_raw)
    materials(root, value, source_directories)
    invocation = dict(
        admission,
        admission_commit=admission_commit,
        admission_file_sha256=sha(admission_raw),
        job_id=env["GITHUB_JOB"],
        private_scan="local_record_not_independently_verified_in_ci",
    )
    return value["plan"], invocation
