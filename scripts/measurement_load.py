"""Fixed-arrival fictional load: retain backlog and every dispatched outcome.

This uses the historical serialized business chain, including scheduled-to-
completion delay. It never catches up by dropping or rescheduling slow samples.
Wall waiting is supplied by the measurement driver, not by product code.
"""

import asyncio
import math

try:
    from .measurement_recording import Recorder
except ImportError:
    from measurement_recording import Recorder


async def requests(recorder, *, count, interval, start, origin, now, wait, operation):
    if (
        type(count) is not int
        or count <= 0
        or type(interval) not in (int, float)
        or not math.isfinite(interval)
        or interval <= 0
        or type(start) not in (int, float)
        or not math.isfinite(start)
        or type(origin) not in (int, float)
        or not math.isfinite(origin)
        or start < origin
    ):
        raise ValueError("measurement_schedule_invalid")
    passed = True
    for index in range(count):
        scheduled = start + index * interval
        entered, outcome = None, "ok"
        try:
            await wait(scheduled)
            entered = now()
            await operation(index)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome = "busy" if getattr(exc, "code", None) == "storage_busy" else "failed"
            passed = False
        finally:
            finished = now()
            recorder.append(
                "requests.jsonl",
                dict(
                    id=index + 1,
                    kind="business",
                    scheduled_s=scheduled - origin,
                    started_s=(finished if entered is None else entered) - origin,
                    finished_s=finished - origin,
                    outcome=outcome,
                ),
            )
    return passed


__all__ = ["Recorder", "requests"]
