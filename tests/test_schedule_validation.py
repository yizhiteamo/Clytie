"""Issue #17：清单与 SDK 对固定周期的接受集合必须相同。"""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from clytie.sdk.schedule import DURATION_PATTERN, duration_seconds
from clytie.sdk.types import ScheduleDeclaration

SCHEMA = json.loads(
    (Path(__file__).parents[1] / "schemas/plugin.schema.json").read_text(encoding="utf-8")
)["properties"]["contributes"]["properties"]["schedule"]["items"]


@pytest.mark.parametrize(
    "every,valid",
    [
        (x, False)
        for x in (
            "P1M",
            "P1Y",
            "P0.5Y",
            "P1M1D",
            "PT0S",
            "P0D",
            "PT0H0M0S",
            "P",
            "PT",
            "P1DT",
            "PT1S\n",
        )
    ]
    + [(x, True) for x in ("P1W", "P1D", "PT1H30M", "PT0.5S", "PT0,5S", "P0.5D")],
)
def test_duration_validation_agrees(every, valid):
    data = dict(id="tick", handler="on_tick", every=every)
    assert Draft202012Validator(SCHEMA).is_valid(data) == valid
    if valid:
        declaration = ScheduleDeclaration.model_validate(data)
        assert ScheduleDeclaration.model_validate_json(declaration.model_dump_json()) == declaration
    else:
        with pytest.raises(ValidationError):
            ScheduleDeclaration.model_validate(data)


@pytest.mark.parametrize("days", [[], [1], [1, 7]])
def test_every_rejects_days_even_empty(days):
    data = dict(id="tick", handler="on_tick", every="PT1S", days=days)
    assert not Draft202012Validator(SCHEMA).is_valid(data)
    with pytest.raises(ValidationError):
        ScheduleDeclaration.model_validate(data)


@pytest.mark.parametrize("extra", [{}, {"days": []}, {"days": [1, 7]}])
def test_at_accepts_days_or_daily_default(extra):
    data = dict(id="tick", handler="on_tick", at="12:30", **extra)
    assert Draft202012Validator(SCHEMA).is_valid(data)
    assert list(ScheduleDeclaration.model_validate(data).days) == extra.get("days", [])


def test_shared_pattern_and_exact_fraction():
    assert SCHEMA["properties"]["every"]["pattern"] == DURATION_PATTERN
    assert duration_seconds("PT1H30M") == 5400
    assert duration_seconds("P1W") == 7 * 24 * 60 * 60
    assert duration_seconds("PT0.1S") * 10 == 1
