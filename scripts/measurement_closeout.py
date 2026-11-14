"""Four fixed closeout scenes; operator checks each sealed result before the next.

This does not reopen the old mechanism/matrix campaign. No retries or automatic
progression. Product trials and encrypted evidence retain their existing checks.
"""

import asyncio
import hashlib
import json
import os
import subprocess
from pathlib import Path

if __package__:
    from . import measurement_evidence as evidence
    from . import measurement_launch as launch
    from . import measurement_mechanism_plan as mechanism
    from . import measurement_plan as plan
    from . import measurement_plan_ingress as ingress
    from . import measurement_preparation_generate as preparation
    from . import measurement_transport as transport
    from . import measurement_version as versions
else:
    import measurement_evidence as evidence
    import measurement_launch as launch
    import measurement_mechanism_plan as mechanism
    import measurement_plan as plan
    import measurement_plan_ingress as ingress
    import measurement_preparation_generate as preparation
    import measurement_transport as transport
    import measurement_version as versions


SCENES = (
    ("Windows", "large_database"),
    ("Windows", "large_asset"),
    ("Linux", "large_database"),
    ("Linux", "large_asset"),
)
ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "a47f509d57dbf947c974363d4b3f52b508a98d09"


def checked(request, runtime, env):
    evidence._fields(request, ("format", "scene", "product", "tool", "prior_verified"))
    if (
        request["format"] != 1
        or type(request["scene"]) is not int
        or not 0 <= request["scene"] < 4
        or request["product"] != PRODUCT
    ):
        raise ValueError("closeout_request_invalid")
    evidence._pattern(request["tool"], r"[a-f0-9]{40}")
    scene = request["scene"]
    expected_prior = list(range(scene))
    if request["prior_verified"] != expected_prior:
        raise ValueError("closeout_prior_missing")
    if (
        env.get("GITHUB_REPOSITORY") != transport.REPOSITORY
        or env.get("GITHUB_RUN_ATTEMPT") != "1"
        or env.get("GITHUB_EVENT_NAME") != "pull_request"
    ):
        raise ValueError("closeout_execution_identity")
    frozen = ingress.unpack((ROOT / ".github/a43-mechanism-plan.json").read_bytes())["plan"]
    system, scale = SCENES[scene]
    if (
        runtime["environment"]["os"] != system
        or plan.digest(runtime) != frozen["inputs"]["inventory"]["environments"][system]
    ):
        raise ValueError("closeout_environment_changed")
    row = next(
        r
        for r in frozen["runs"]
        if r["platform"] == system and r["scale"] == scale and r["scenario"] == "normal"
    )
    row["source_commit"] = PRODUCT
    row["execution_id"] = plan.digest(request)
    load = row["window"]["load"]
    # Shift the entire prior post-start budget; do not shrink the backup window.
    load["backup_start_seconds"] = 120
    load["window_seconds"] += 119.9
    load["maximum_seconds"] += 119.9
    load["request_count"] += 4796
    tools = launch.tool_inventory(ROOT)
    for name in (
        "scripts/measurement_closeout.py",
        ".github/workflows/closeout-windows.yml",
        ".github/workflows/closeout-linux.yml",
    ):
        tools["files"][name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    tools["sha256"] = plan.digest(tools["files"])
    frozen["inputs"]["inventory"].update(candidate=PRODUCT, tool_sha256=tools["sha256"])
    frozen["plan_sha256"] = plan.digest(dict(request=request, load=load))
    result = mechanism._manifest(
        frozen,
        row,
        runtime=runtime,
        workflow=dict(run_id=int(env["GITHUB_RUN_ID"]), attempt=1, job_id=env["GITHUB_JOB"]),
        phase="closeout",
    )
    return result


def main():
    env = os.environ
    request = ingress.unpack((Path(env["RUNNER_TEMP"]) / "closeout-request.json").read_bytes())
    event = json.loads(Path(env["GITHUB_EVENT_PATH"]).read_bytes())
    if (
        event.get("action") != "synchronize"
        or event.get("number") != 71
        or event["pull_request"]["head"]["repo"]["full_name"] != transport.REPOSITORY
    ):
        raise ValueError("closeout_event_invalid")
    actual = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT).decode().strip()
    if actual != request["tool"]:
        raise ValueError("closeout_tool_changed")
    manifest = checked(request, preparation.runtime(), env)
    public = json.loads((ROOT / ".github/a43-preparation-public.json").read_bytes())
    if (
        public["fingerprint"] != evidence.fingerprint(public["recipient"])
        or public["fingerprint"]
        != "09d84206c57a4adb3efd462881c0801b7cd9fcadcf103d176ff3b6915fc31eaf"
    ):
        raise ValueError("closeout_key_changed")
    base = Path(env["RUNNER_TEMP"]).resolve(strict=True)
    root = base / "a43-closeout"
    root.mkdir(exist_ok=False)
    versions.prepare(
        ROOT, root / "product", mode="candidate", candidate=PRODUCT, tool_commit=request["tool"]
    )
    source, _ = versions.activate(root / "product")
    if __package__:
        from .measurement_trial import _execute
    else:
        from measurement_trial import _execute
    # _execute enforces real production capture, complete backup and actual release.
    # A pass here remains a driver result, not the human phase/P99 acceptance.
    failed = None
    try:
        asyncio.run(_execute(root / "trial", manifest, source, tiny=False))
    except BaseException as exc:
        failed = type(exc).__name__
    raw = root / "trial/raw"
    actual_manifest = json.loads((raw / "run-manifest.json").read_bytes())
    evidence.seal(raw, root / "sealed", public["recipient"], manifest)
    paths = transport.validate_sealed(root / "sealed")
    with Path(env["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as out:
        out.write(f"ciphertext={paths[0]}\nreceipt={paths[1]}\n")
    if failed or actual_manifest["result"] != "pass":
        raise SystemExit("closeout_trial_failed_retained_ciphertext")


if __name__ == "__main__":
    main()
