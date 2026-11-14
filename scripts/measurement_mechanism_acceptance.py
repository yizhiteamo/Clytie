"""Local evidence gate; no execution, dispatch, retry, or performance claims.

The receipt records a trusted local review, not a cryptographic signature. CI
cannot independently establish the private scan or the human coverage judgment.
Original aggregate reports are retained unchanged alongside their resolutions.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

if __package__:
    from . import measurement_cancel_validation as cancellation
    from . import measurement_evidence as evidence
    from . import measurement_mechanism_plan as mechanism
    from . import measurement_mechanism_validation as validation
    from . import measurement_plan as plan
    from . import measurement_plan_ingress as ingress
    from . import measurement_transport as transport
else:
    import measurement_cancel_validation as cancellation
    import measurement_evidence as evidence
    import measurement_mechanism_plan as mechanism
    import measurement_mechanism_validation as validation
    import measurement_plan as plan
    import measurement_plan_ingress as ingress
    import measurement_transport as transport


Error = evidence.EvidenceError
TRUST = "local_operator_ci_metadata_and_human_source_coverage_not_machine_proof"
BINDINGS = ("candidate", "fixture_sha256", "tool_sha256", "plan_sha256", "results_sha256")
COMBINATIONS = {
    "host_io": ("test_backup_io_files", "test_backup_assets_integration"),
    "snapshot": ("test_backup_io_snapshot", "test_measurement_pressure_observation"),
    "native": ("test_backup_native_validation", "test_measurement_native_context"),
    "management": ("test_management_operation_slot", "test_backup_memory_io_channel"),
    "control": (
        "test_backup_io_control",
        "test_backup_io_admission_control",
        "test_backup_io_restart_wiring",
    ),
    "restart": ("test_backup_io_restart_http", "test_backup_io_responsibility"),
    "pressure": ("test_backup_io_budget", "test_measurement_pressure_validation"),
    "views": ("test_backup_view_limits", "test_measurement_views"),
    "cancellation": ("test_measurement_cancel_trial", "test_backup_worker_cleanup"),
}


def verbose_test_results(raw):
    """Strict complete pytest -v transcript; raw CI timestamps may prefix lines.

    Node ids are evidence data, never commands. A final summary must account for
    every observed test, including skips. Collection/worker failures cannot pass.
    """
    rows = {}
    summary = None
    for line in raw.decode("utf-8").splitlines():
        line = re.sub(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z\s+", "", line)
        match = re.fullmatch(
            r"((?:tests|plugins)[/\\].+?)\s+(PASSED|SKIPPED|FAILED|ERROR|XFAIL|XPASS)"
            r"(?:\s+\([^\r\n]*\))?"
            r"(?:\s+\[\s*\d+%\])?\s*",
            line,
        )
        if match:
            name, status = match.groups()
            require(name not in rows, "duplicate_test_result")
            rows[name] = status
        if re.match(r"=+ .* in [0-9.]+s(?: \([^)]+\))? =+$", line):
            require(summary is None, "multiple_test_summaries")
            summary = {
                name: int(number)
                for number, name in re.findall(
                    r"(\d+) (passed|skipped|failed|error|errors|xfailed|xpassed)", line
                )
            }
    require(bool(rows) and summary is not None, "test_transcript_incomplete")
    names = {
        "PASSED": "passed",
        "SKIPPED": "skipped",
        "FAILED": "failed",
        "ERROR": "error",
        "XFAIL": "xfailed",
        "XPASS": "xpassed",
    }
    counts = {name: list(rows.values()).count(status) for status, name in names.items()}
    summary["error"] = summary.pop("errors", summary.get("error", 0))
    require(
        all(summary.get(name, 0) == count for name, count in counts.items()),
        "test_transcript_count_mismatch",
    )
    require(
        not any(counts[name] for name in ("failed", "error", "xfailed", "xpassed")),
        "combination_tests_failed_or_missing",
    )
    passed = set()
    for name, status in rows.items():
        if status == "PASSED":
            module, separator, case = name.partition("::")
            require(bool(separator) and module.endswith(".py"), "test_node_invalid")
            # Normalize only the module path. Parametrized values are test identity:
            # converting their slashes can merge a skipped case with a passed one.
            passed.add(module[:-3].replace("\\", "/").replace("/", ".") + "::" + case)
    require(len(passed) == counts["passed"], "test_identity_collision")
    return passed


def require(condition, code):
    if not condition:
        raise Error(code)


class Bundle:
    """All evidence references are relative, hashed regular files in one bundle."""

    def __init__(self, path):
        self.path = evidence._safe_path(Path(path).absolute())
        self.root = self.path.parent
        self.seen = {self.path: evidence._hash(self.path)}
        self.value = evidence._json(self.path.read_bytes())

    def path_for(self, name):
        require(isinstance(name, str) and bool(name), "evidence_path_invalid")
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, "evidence_path_invalid")
        path = evidence._safe_path(self.root / relative)
        require(path.resolve().is_relative_to(self.root.resolve()), "evidence_path_escape")
        return path

    def file(self, ref):
        evidence._fields(ref, ("path", "sha256"))
        path = self.path_for(ref["path"])
        require(path.is_file() and path.stat().st_nlink == 1, "evidence_file_invalid")
        require(evidence._hash(path) == ref["sha256"], "evidence_hash_mismatch")
        self.seen[path] = ref["sha256"]
        return path

    def material_path(self, name):
        """Explicit local Git/tool/source directories may be outside the bundle.

        They must be dedicated public code trees, never a deployed instance. The
        source/archive and tool validators verify their complete inventories.
        """
        require(isinstance(name, str) and bool(name), "material_path_invalid")
        path = Path(name)
        return evidence._safe_path(path if path.is_absolute() else self.path_for(name))

    def json(self, ref):
        return evidence._json(self.file(ref).read_bytes())

    def unchanged(self):
        require(
            all(evidence._hash(path) == digest for path, digest in self.seen.items()),
            "evidence_changed_during_verification",
        )


def _provenance(bundle, frozen, root):
    """Recheck P/Q/B Git objects and saved actual CI metadata, without network."""
    value = bundle.value
    admission_commit = value["admission_commit"]
    ingress._hash(admission_commit, 40)
    admission = ingress.unpack(ingress.git_file(root, admission_commit, ingress.ADMISSION_PATH))
    require(
        ingress.only_change(root, admission_commit, ingress.ADMISSION_PATH)
        == admission["request_commit"],
        "admission_parent_mismatch",
    )
    request = ingress.request_at(root, admission["request_commit"])
    payload, raw, record = ingress.pair(root, request["plan_commit"])
    require(payload["plan"] == frozen, "provenance_plan_mismatch")
    ingress.preparation.material_identity(
        root, bundle.material_path(value["version_directory"]), request["product_commit"]
    )
    _, actual_tools = mechanism.verify_materials(
        bundle.material_path(value["version_directory"]), bundle.material_path(value["tool_root"])
    )
    for name, digest in actual_tools["files"].items():
        require(
            ingress.sha(ingress.versions._git(root, "show", payload["tool_commit"] + ":" + name))
            == digest,
            "tool_commit_bytes_mismatch",
        )
    require(
        admission
        == dict(
            request,
            purpose="fixed_run_admission",
            request_commit=admission["request_commit"],
            run_id=admission["run_id"],
            attempt=1,
            jobs=ingress.JOBS,
        ),
        "admission_mismatch",
    )
    require(
        request["plan_file_sha256"] == ingress.sha(raw)
        and request["record_sha256"] == ingress.sha(record)
        and request["product_commit"] == frozen["inputs"]["inventory"]["candidate"]
        and request["tool_commit"] == payload["tool_commit"],
        "provenance_binding_mismatch",
    )
    ci = bundle.json(value["ci_run"])
    require(
        ci.get("id") == admission["run_id"]
        and ci.get("run_attempt") == 1
        and ci.get("head_sha") == admission["request_commit"]
        and ci.get("event") == "pull_request"
        and ci.get("path") == ingress.WORKFLOW_PATH
        and ci.get("repository", {}).get("full_name") == transport.REPOSITORY
        and ci.get("head_repository", {}).get("full_name") == transport.REPOSITORY
        and ci.get("status") == "completed"
        and ci.get("conclusion") == "success",
        "ci_run_not_complete",
    )
    return admission


def _test_evidence(bundle, frozen, root):
    """Parse actual JUnit, including every failure and skip, rather than a pass flag."""
    inventory = frozen["inputs"]["inventory"]
    suites = bundle.value["tests"]
    require(set(suites) == set(plan.PLATFORMS), "test_platforms_incomplete")
    passed = {}
    for platform, item in suites.items():
        evidence._fields(
            item,
            (
                "candidate",
                "environment",
                "command",
                "results",
                "result_format",
                "result_source",
                "ci",
                "job",
                "sources",
            ),
        )
        require(
            item["candidate"] == inventory["candidate"]
            and isinstance(item["command"], list)
            and bool(item["command"])
            and all(isinstance(arg, str) and arg for arg in item["command"]),
            "test_candidate_mismatch",
        )
        ci = bundle.json(item["ci"])
        ingress._hash(ci.get("head_sha"), 40)
        require(
            ci.get("status") == "completed"
            and ci.get("conclusion") == "success"
            and isinstance(ci.get("id"), int)
            and ci["id"] > 0
            and ci.get("repository", {}).get("full_name") == transport.REPOSITORY,
            "test_ci_mismatch",
        )
        environment = bundle.json(item["environment"])
        require(environment.get("platform") == platform, "test_environment_mismatch")
        job = bundle.json(item["job"])
        require(
            job.get("run_id") == ci["id"]
            and job.get("head_sha") == ci["head_sha"]
            and type(job.get("id")) is int
            and job["id"] > 0
            and job.get("status") == "completed"
            and job.get("conclusion") == "success"
            and any(
                step.get("name") == "Offline tests" and step.get("conclusion") == "success"
                for step in job.get("steps", [])
            ),
            "test_job_mismatch",
        )
        # A later tool-only CI head is admissible only with actual product tree
        # equality, not a claim that the candidate was tested.
        diff = subprocess.run(
            [
                "git",
                "diff",
                "--quiet",
                inventory["candidate"],
                ci["head_sha"],
                "--",
                "src",
                "plugins",
                "schemas",
                "pyproject.toml",
                "uv.lock",
            ],
            cwd=root,
            capture_output=True,
            check=False,
        )
        require(diff.returncode == 0, "test_product_tree_mismatch")
        require(isinstance(item["sources"], dict) and bool(item["sources"]), "test_sources_missing")
        for name, digest in item["sources"].items():
            require(
                name.startswith("tests/") and ".." not in Path(name).parts,
                "test_source_path_invalid",
            )
            found = subprocess.run(
                ["git", "show", ci["head_sha"] + ":" + name],
                cwd=root,
                capture_output=True,
                check=False,
            )
            require(
                found.returncode == 0 and ingress.sha(found.stdout) == digest,
                "test_source_mismatch",
            )
        raw = bundle.file(item["results"]).read_bytes()
        if item["result_format"] == "pytest_verbose":
            origin = item["result_source"]
            evidence._fields(origin, ("archive", "entry"))
            step = [step for step in job["steps"] if step.get("name") == "Offline tests"]
            require(len(step) == 1 and type(step[0].get("number")) is int, "test_step_unbound")
            entry = origin["entry"]
            require(
                isinstance(entry, str)
                and not Path(entry).is_absolute()
                and ".." not in Path(entry).parts
                and Path(entry).name == str(step[0]["number"]) + "_Offline tests.txt",
                "test_log_entry_invalid",
            )
            with zipfile.ZipFile(bundle.file(origin["archive"])) as logs:
                require(
                    logs.namelist().count(entry) == 1 and logs.getinfo(entry).file_size == len(raw),
                    "test_log_entry_mismatch",
                )
                require(logs.read(entry) == raw, "test_log_bytes_mismatch")
            command = " ".join(item["command"])
            require(
                item["command"] == ["uv", "run", "--no-sync", "pytest", "-v"]
                and ("##[group]Run " + command) in raw.decode("utf-8"),
                "test_command_unbound",
            )
            passed[platform] = verbose_test_results(raw)
        else:
            require(item["result_format"] == "junit", "test_result_format")
            require(item["result_source"] is None, "junit_source_format")
            require(b"<!DOCTYPE" not in raw and b"<!ENTITY" not in raw, "junit_invalid")
            tree = ET.fromstring(raw)
            cases = list(tree.iter("testcase"))
            require(
                bool(cases) and not list(tree.iter("failure")) and not list(tree.iter("error")),
                "combination_tests_failed_or_missing",
            )
            passed[platform] = {
                case.get("classname", "") + "::" + case.get("name", "")
                for case in cases
                if case.find("skipped") is None
            }
        require(bool(passed[platform]), "combination_tests_not_run")
        for modules in COMBINATIONS.values():
            for module in modules:
                require("tests/" + module + ".py" in item["sources"], "combination_source_missing")
    # Every documented combination family needs actual executed cases. Platform
    # skips are retained; a skipped case cannot satisfy a human coverage mapping.
    all_passed = set().union(*passed.values())
    for modules in COMBINATIONS.values():
        for module in modules:
            require(
                any(case.startswith("tests." + module + "::") for case in all_passed),
                "required_combination_not_run",
            )
    require(
        any(
            case.startswith(
                "tests.test_backup_io_restart_http::"
                "test_real_daemon_restart_blocks_orphan_io_but_other_companion_chats"
            )
            for case in all_passed
        ),
        "real_restart_combination_not_run",
    )
    return passed


def _coverage(bundle, reports, passed, source):
    review = bundle.json(bundle.value["coverage"])
    evidence._fields(review, ("candidate", "trust_boundary", "reviewer", "requirements"))
    require(
        review["candidate"] == source["candidate"]
        and review["trust_boundary"] == TRUST
        and isinstance(review["reviewer"], str)
        and bool(review["reviewer"]),
        "coverage_review_missing",
    )
    unknown = {}

    def findings(value, prefix=""):
        if isinstance(value, dict):
            if "status" in value:
                yield prefix, value
            for key, child in value.items():
                yield from findings(child, key if not prefix else prefix + "." + key)
        elif isinstance(value, list):
            for number, child in enumerate(value):
                yield from findings(child, prefix + "." + str(number))

    for report in reports.values():
        require(report["mechanism_acceptance"] == "not_evaluated", "aggregate_contract_changed")
        for name, finding in findings(report):
            require(finding["status"] != "failed", "observed_mechanism_failed")
            if finding["status"] == "unverified":
                unknown.setdefault(name, set()).update(
                    finding.get(
                        "reasons",
                        [
                            finding.get(
                                "reason", "named_aggregate_predicate_requires_external_evidence"
                            )
                        ],
                    )
                )
    require(set(review["requirements"]) == set(unknown), "coverage_requirements_incomplete")
    for name, reasons in unknown.items():
        item = review["requirements"][name]
        evidence._fields(item, ("reasons", "judgment", "rationale", "tests", "sources"))
        require(
            set(item["reasons"]) == reasons
            and item["judgment"] == "covered"
            and isinstance(item["rationale"], str)
            and bool(item["rationale"]),
            "coverage_not_resolved",
        )
        require(set(item["tests"]) == set(plan.PLATFORMS), "coverage_platform_missing")
        for platform, cases in item["tests"].items():
            require(
                isinstance(cases, list) and bool(cases) and set(cases) <= passed[platform],
                "coverage_test_missing_or_skipped",
            )
        require(
            isinstance(item["sources"], list) and bool(item["sources"]), "coverage_source_missing"
        )
        for ref in item["sources"]:
            evidence._fields(ref, ("path", "sha256", "lines"))
            require(
                source["files"].get(ref["path"]) == ref["sha256"]
                and isinstance(ref["lines"], list)
                and len(ref["lines"]) == 2
                and all(type(n) is int and n > 0 for n in ref["lines"])
                and ref["lines"][0] <= ref["lines"][1],
                "coverage_source_mismatch",
            )
            actual = (
                bundle.material_path(bundle.value["version_directory"]) / "source" / ref["path"]
            )
            require(
                ref["lines"][1] <= len(actual.read_bytes().splitlines()),
                "coverage_source_lines_invalid",
            )
    return review


def verify_public_record(value, expected=None):
    """Validate a pinned receipt's shape/bindings; does NOT prove its local execution."""
    evidence._fields(value, ("format", "accepted", *BINDINGS, "trust_boundary"))
    require(
        type(value["format"]) is int
        and value["format"] == 1
        and value["accepted"] is True
        and value["trust_boundary"] == TRUST,
        "mechanism_receipt_invalid",
    )
    for name in BINDINGS:
        ingress._hash(value[name], 40 if name == "candidate" else 64)
        if expected is not None:
            require(value[name] == expected[name], "mechanism_receipt_binding_mismatch")
    return value


def _execution_exit(bundle, item, admission, row, ordinal):
    evidence._fields(item, ("job", "log", "archive", "entry"))
    job = bundle.json(item["job"])
    require(
        job.get("run_id") == admission["run_id"]
        and job.get("head_sha") == admission["request_commit"]
        and job.get("name") == admission["jobs"][row["platform"]]
        and job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and type(job.get("id")) is int
        and job["id"] > 0,
        "execution_job_mismatch",
    )
    name = f"Execute frozen platform slot {ordinal + 1} once and seal stable output"
    steps = [step for step in job.get("steps", []) if step.get("name") == name]
    require(
        len(steps) == 1
        and type(steps[0].get("number")) is int
        and steps[0].get("status") == "completed"
        and steps[0].get("conclusion") == "success",
        "execution_step_missing",
    )
    entry = item["entry"]
    require(
        isinstance(entry, str)
        and not Path(entry).is_absolute()
        and ".." not in Path(entry).parts
        and Path(entry).name == str(steps[0]["number"]) + "_" + name + ".txt",
        "execution_log_entry_invalid",
    )
    raw = bundle.file(item["log"]).read_bytes()
    with zipfile.ZipFile(bundle.file(item["archive"])) as logs:
        require(
            logs.namelist().count(entry) == 1
            and logs.getinfo(entry).file_size == len(raw)
            and logs.read(entry) == raw,
            "execution_log_bytes_mismatch",
        )
    lines = [
        re.sub(r"^\d{4}-\d\d-\d\dT[0-9:.]+Z\s+", "", line)
        for line in raw.decode("utf-8").splitlines()
    ]
    command = (
        "##[group]Run uv run --no-sync python -B -m "
        f"scripts.measurement_mechanism_workflow run-slot --slot {ordinal}"
    )
    marker = (
        "mechanism_stage=sealed_pending_local_verification driver_exit_confirmed=true "
        "ciphertext_saved=true artifact_upload=not_observed worker_exit=not_evaluated_here"
    )
    require(
        lines.count(command) == 1
        and lines.count(marker) == 1
        and lines.count("mechanism_slot_pending_local_verification") == 1
        and not any(
            "mechanism_slot_failed_no_retry" in line
            or "mechanism_workflow_rejected_no_retry" in line
            for line in lines
        ),
        "execution_exit_unverified",
    )


def verify_record(record_path, *, identity=None, private_scan=None, target=None):
    """Authenticate all eight archives and resolve only explicit unverified findings.

    ``identity`` is an already loaded dedicated age identity. No key is discovered.
    ``private_scan`` is injectable solely for offline tests; production defaults to
    the existing local scanner. A fresh persistent directory retains all reports,
    including failed checks. Nothing is deleted or overwritten by this gate.
    """
    transport._local_only()
    require(identity is not None, "mechanism_identity_required")
    if target is None:
        target = Path(record_path).absolute().parent / ("verification-" + uuid.uuid4().hex)
    output = evidence._new_directory(target)
    state = {"accepted": False, "state": "verification_incomplete"}
    try:
        result = _verify(record_path, identity, private_scan or evidence.private_scan, output)
        state = result
        return result
    finally:
        evidence._save(output / "checkpoint.json", state)


def _verify(record_path, identity, private_scan, output):
    bundle = Bundle(record_path)
    value = bundle.value
    evidence._fields(
        value,
        (
            "format",
            "plan",
            "repository",
            "version_directory",
            "tool_root",
            "admission_commit",
            "ci_run",
            "runs",
            "tests",
            "coverage",
        ),
    )
    require(type(value["format"]) is int and value["format"] == 1, "record_format")
    frozen = bundle.json(value["plan"])
    plan.validate_plan(frozen)
    require(
        frozen["inputs"]["kind"] == "mechanism" and len(frozen["runs"]) == 8,
        "complete_mechanism_plan_required",
    )
    version_directory = bundle.material_path(value["version_directory"])
    tool_root = bundle.material_path(value["tool_root"])
    source, tools = mechanism.verify_materials(version_directory, tool_root)
    mechanism._check_materials(frozen, source, tools)
    admission = _provenance(bundle, frozen, bundle.material_path(value["repository"]))
    require(
        set(value["runs"]) == {row["execution_id"] for row in frozen["runs"]},
        "eight_executions_required",
    )
    reports = {}
    for number, row in enumerate(frozen["runs"]):
        item = value["runs"][row["execution_id"]]
        evidence._fields(
            item,
            (
                "runtime",
                "ciphertext",
                "receipt",
                "download",
                "execution",
                "artifact_metadata",
                "downloaded_zip",
            ),
        )
        runtime = bundle.json(item["runtime"])
        expected = mechanism.prepare_manifest(
            frozen,
            row["execution_id"],
            source_inventory=source,
            tools=tools,
            runtime=runtime,
            workflow=dict(
                run_id=admission["run_id"], attempt=1, job_id=admission["jobs"][row["platform"]]
            ),
        )
        cipher = bundle.file(item["ciphertext"])
        receipt = bundle.file(item["receipt"])
        require(
            receipt.parent == cipher.parent
            and cipher.name == "measurements.age"
            and receipt.name == "receipt.json",
            "sealed_layout_invalid",
        )
        transport.validate_sealed(cipher.parent)
        download = bundle.json(item["download"])
        meta = bundle.json(item["artifact_metadata"])
        retained = transport.retention(meta, run_id=admission["run_id"])
        platform_rows = [run for run in frozen["runs"] if run["platform"] == row["platform"]]
        ordinal = platform_rows.index(row)
        artifact_name = (
            f"a43-encrypted-{admission['run_id']}-1-{row['platform'].lower()}-slot{ordinal}"
        )
        require(
            meta.get("name") == artifact_name
            and download.get("artifact_name") == artifact_name
            and all(
                download.get(key) == retained[key]
                for key in (
                    "artifact_id",
                    "run_id",
                    "requested_days",
                    "accepted_days",
                    "created_at",
                    "expires_at",
                )
            ),
            "artifact_identity_mismatch",
        )
        downloaded = bundle.file(item["downloaded_zip"])
        require(
            evidence._hash(downloaded) == download.get("downloaded_sha256"),
            "downloaded_zip_mismatch",
        )
        with zipfile.ZipFile(downloaded) as archive_zip:
            require(
                len(archive_zip.infolist()) == 2
                and set(archive_zip.namelist()) == {"measurements.age", "receipt.json"},
                "downloaded_zip_entries",
            )
            for actual in (cipher, receipt):
                import hashlib

                digest = hashlib.sha256()
                with archive_zip.open(actual.name) as stream:
                    total = 0
                    while block := stream.read(256 * 1024):
                        total += len(block)
                        require(total <= actual.stat().st_size, "downloaded_zip_size")
                        digest.update(block)
                require(
                    total == actual.stat().st_size and digest.hexdigest() == evidence._hash(actual),
                    "downloaded_zip_content_mismatch",
                )
        require(
            download["run_id"] == admission["run_id"]
            and download["ciphertext_sha256"] == evidence._hash(cipher)
            and download["requested_days"] == 14
            and download["accepted_days"] > 0
            and isinstance(download["artifact_id"], int)
            and download["artifact_id"] > 0
            and bool(download["created_at"])
            and bool(download["expires_at"]),
            "artifact_handoff_unverified",
        )
        _execution_exit(bundle, item["execution"], admission, row, ordinal)
        opened = output / f"run-{number}"
        archive = evidence.open_bundle(
            cipher, opened, identity, expected, private_scan=private_scan
        )
        require(
            archive["accepted"] is True and archive["measurement_result"] == row["expected_result"],
            "archive_or_measurement_ineligible",
        )
        if row["scenario"] == "cancel":
            checked = cancellation.verify_cancel_evidence(
                evidence._read_sources(opened / "raw", format_version=6), expected
            )
            evidence._save(output / f"planned-cancel-{number}.json", checked)
        report = validation.aggregate(opened / "raw", output / f"analysis-{number}", expected)
        evidence._save(output / f"aggregate-{number}.json", report)
        reports[row["execution_id"]] = report
    passed = _test_evidence(bundle, frozen, bundle.material_path(value["repository"]))
    review = _coverage(bundle, reports, passed, source)
    # Full CI metadata/JUnit remain local, including any generic findings. This
    # does not grant those raw materials publication or an expanded exception.
    # Only the minimal receipt is a publication candidate. Human review prose
    # must also pass the unchanged scanner before it can support that receipt.
    generic = {}
    scanner = evidence.public_scanner()
    for path in bundle.seen:
        if path.suffix not in {".age", ".zip"}:
            categories = set()
            for line in path.read_text(encoding="utf-8").splitlines():
                categories.update(scanner(line))
            generic[path.relative_to(bundle.root).as_posix()] = sorted(categories)
    ingress.public_scan(bundle.file(value["coverage"]).read_bytes())
    support = [path for path in bundle.seen if path.suffix not in {".age", ".zip"}]
    require(private_scan(support) == "pass", "supporting_private_scan_failed")
    evidence._save(
        output / "supporting-scan-results.json",
        dict(generic=generic, private_scan="pass", publication="local_only"),
    )
    bundle.unchanged()
    after_source, after_tools = mechanism.verify_materials(version_directory, tool_root)
    require(source == after_source and tools == after_tools, "materials_changed")
    inventory = frozen["inputs"]["inventory"]
    result = dict(
        format=1,
        accepted=True,
        candidate=inventory["candidate"],
        fixture_sha256=inventory["fixture_sha256"],
        tool_sha256=inventory["tool_sha256"],
        plan_sha256=frozen["plan_sha256"],
        trust_boundary=TRUST,
        results_sha256=plan.digest(
            dict(
                reports=reports,
                review=review,
                scans=generic,
                evidence={
                    path.relative_to(bundle.root).as_posix(): digest
                    for path, digest in bundle.seen.items()
                },
            )
        ),
    )
    verify_public_record(result)
    evidence._save(output / "receipt.json", result)
    ingress.public_scan((output / "receipt.json").read_bytes())
    require(private_scan([output / "receipt.json"]) == "pass", "receipt_private_scan_failed")
    return result


def prepare_record(plan_path, target):
    """Create an explicitly pending local template; no collection or admission."""
    transport._local_only()
    raw = evidence._safe_path(plan_path).read_bytes()
    frozen = evidence._json(raw)
    plan.validate_plan(frozen)
    require(frozen["inputs"]["kind"] == "mechanism", "mechanism_plan_required")
    output = evidence._new_directory(target)
    with (output / "plan.json").open("xb") as stream:
        stream.write(raw)

    def ref(name):
        return dict(path=name, sha256=None)

    template = dict(
        format=1,
        plan=dict(path="plan.json", sha256=ingress.sha(raw)),
        repository=None,
        version_directory=None,
        tool_root=None,
        admission_commit=None,
        ci_run=ref("mechanism-ci.json"),
        coverage=ref("coverage.json"),
        runs={},
        tests={},
    )
    for i, row in enumerate(frozen["runs"]):
        prefix = f"runs/slot-{i}/"
        template["runs"][row["execution_id"]] = {
            "runtime": ref(prefix + "runtime.json"),
            "ciphertext": ref(prefix + "sealed/measurements.age"),
            "receipt": ref(prefix + "sealed/receipt.json"),
            "download": ref(prefix + "download.json"),
            "execution": dict(
                job=ref(prefix + "job.json"),
                log=ref(prefix + "slot.txt"),
                archive=ref("mechanism-logs.zip"),
                entry=None,
            ),
            "artifact_metadata": ref(prefix + "artifact-metadata.json"),
            "downloaded_zip": ref(prefix + "artifact.zip"),
        }
    for platform in plan.PLATFORMS:
        prefix = f"tests/{platform}/"
        template["tests"][platform] = dict(
            candidate=frozen["inputs"]["inventory"]["candidate"],
            environment=ref(prefix + "environment.json"),
            command=["uv", "run", "--no-sync", "pytest", "-v"],
            results=ref(prefix + "offline-tests.txt"),
            result_format="pytest_verbose",
            result_source=dict(archive=ref(prefix + "logs.zip"), entry=None),
            ci=ref(prefix + "ci.json"),
            job=ref(prefix + "job.json"),
            sources={},
        )
    evidence._save(output / "record.json", template)
    evidence._save(
        output / "checkpoint.json",
        dict(
            accepted=False,
            state="pending_actual_evidence",
            missing=[
                "authentic_eight_archives",
                "fixed_git_provenance",
                "ci_and_combination_results",
                "human_source_coverage",
                "actual_local_scans",
            ],
        ),
    )
    return template


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path)
    parser.add_argument("--identity", type=Path)
    parser.add_argument("--prepare-plan", type=Path)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.prepare_plan is not None:
            require(args.record is None and args.identity is None, "prepare_arguments_invalid")
            prepare_record(args.prepare_plan, args.target)
            print("mechanism_evidence_pending")
            return 0
        require(args.record is not None and args.identity is not None, "verify_arguments_required")
        verify_record(
            args.record, identity=transport.load_identity(args.identity), target=args.target
        )
    except (ValueError, OSError, KeyError, TypeError, ET.ParseError):
        print("mechanism_verification_incomplete")
        return 1
    print("mechanism_local_verification_complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
