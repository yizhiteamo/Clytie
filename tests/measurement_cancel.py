"""Deterministic capture-entry cancellation and content-free failure diagnosis."""

import asyncio
import json

_CODES = {
    "measurement_cleanup_wait_cancelled",
    "measurement_cleanup_cancelled",
    "measurement_cleanup_failed",
    "measurement_maximum_exceeded",
    "measurement_sampler_failed",
    "measurement_backup_window_uncovered",
    "measurement_trial_operation_failed",
    "measurement_owner_exit_unconfirmed",
    "measurement_budget_not_released",
    "measurement_view_release_unconfirmed",
    "measurement_worker_exit_unconfirmed",
    "measurement_worker_failed",
    "measurement_observer_unsettled",
}


def diagnosis(task, started, root):
    result = dict(capture_entered=started.is_set(), task="active", codes=[])
    if task.cancelled():
        result["task"] = "cancelled"
    elif task.done():
        if task.exception() is not None:
            result["task"] = "exception"
        else:
            value = task.result()
            result["task"] = "returned"
            outcome = value.get("result") if isinstance(value, dict) else None
            result["result"] = outcome if outcome in {"pass", "fail", "incomplete"} else "other"
    path = root / "trial-status.json"
    result["status_exists"] = path.exists()
    if path.exists():
        try:
            codes = json.loads(path.read_bytes())["codes"]
            if not isinstance(codes, list) or not all(isinstance(code, str) for code in codes):
                raise ValueError("invalid_status")
            result["codes"] = sorted({code if code in _CODES else "unknown_code" for code in codes})
        except (OSError, ValueError, KeyError, TypeError):
            result["codes"] = ["status_unreadable"]
    return json.dumps(result, sort_keys=True)


async def cancel_at_capture(task, started, proceed, root):
    waiting = asyncio.create_task(started.wait())
    try:
        await asyncio.wait((task, waiting), return_when=asyncio.FIRST_COMPLETED)
        # FIRST_COMPLETED may contain both tasks. A completed trial remains a
        # failure, but is not evidence that capture was never entered.
        if task.done() or not started.is_set():
            raise AssertionError("trial_not_active_at_cancel: " + diagnosis(task, started, root))
        assert task.cancel()
        # Cleanup joins the capture bridge; release its test gate BEFORE waiting
        # for the cancelled trial, without a sleep or a longer deadline.
        proceed.set()
        await task
        raise AssertionError("trial_suppressed_cancel")
    finally:
        proceed.set()
        waiting.cancel()
        if not task.done():
            task.cancel()
        await asyncio.gather(waiting, task, return_exceptions=True)
