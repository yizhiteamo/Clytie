import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from .test_api_management import management
from .test_backup_explicit_cleanup import fixture


def validator(kind):
    schema = json.loads(Path("schemas/backup-cleanup.schema.json").read_text())
    schema["$ref"] = "#/$defs/" + kind
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.mark.parametrize(
    "body,valid",
    [
        ({"backup_ids": ["package_a", "package_b"]}, True),
        ({"category": "regular", "backup_ids": ["1" * 32]}, True),
        ({"backup_ids": ["../other"]}, False),
        ({"backup_ids": ["*"]}, False),
        ({"backup_ids": ["a", "a"]}, False),
        ({"backup_ids": ["a"], "source_repository": {}}, False),
        ({"backup_ids": []}, False),
    ],
)
def test_explicit_cleanup_request_schema(body, valid):
    assert validator("plan_request").is_valid(body) is valid


async def test_public_plan_schema_uses_actual_plan_and_omits_internal_file_identity(tmp_path):
    storage, cleanup = fixture(tmp_path)
    plan = cleanup.plan("regular", backup_ids=["package_0", "package_4", "foreign_a"])
    api = management(storage.home)
    value = await api.call(
        "/backup/cleanup/plans/<plan_id>", {"plan_id": plan["id"]}, {}, {}, "GET"
    )
    validator("plan_response").validate(value)
    assert "record" not in value["items"][0]
    assert "path" not in value["items"][0]
    assert value["eligible_ids"] == ["package_0"]
    assert value["items"][2]["reason"] == "backup_not_owned"
