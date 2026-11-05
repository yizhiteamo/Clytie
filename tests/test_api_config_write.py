import pytest

from clytie.api.auth import APIError
from clytie.api.config_write import ConfigWriter, patch_source
from clytie.sdk.testing import MockClock


def test_scalar_patch_preserves_all_other_bytes():
    before = "# heading\r\nschema_version: 1\r\nplugins:\r\n  echo-tool: # note\r\n    enabled: true # toggle\r\n    config: {text: 'harmless'}\r\n"
    assert patch_source(before, {"plugins": {"echo-tool": {"enabled": False}}}) == before.replace(
        "enabled: true", "enabled: false"
    )


@pytest.mark.parametrize(
    "source",
    ["plugins: &alias {}\n", "plugins:\n  echo-tool: {enabled: true}\n", "plugins:\n  <<: {}\n"],
)
def test_unsafe_yaml_is_not_reformatted(source):
    with pytest.raises(APIError, match="config_not_writable"):
        patch_source(source, {"plugins": {"echo-tool": {"enabled": False}}})


def test_history_retention_and_invalid_write_restore(tmp_path):
    clock = MockClock()
    path = tmp_path / "companions/companion_a/companion.yaml"
    path.parent.mkdir(parents=True)
    original = "plugins:\n  echo-tool:\n    enabled: true\n"
    path.write_text(original, encoding="utf-8")
    writer = ConfigWriter(tmp_path, clock, validate=lambda: True)
    for index in range(12):
        writer.save("companion_a", {"plugins": {"echo-tool": {"enabled": bool(index % 2)}}})
        clock.advance(1)
    assert len(list((path.parent / "config-history").iterdir())) == 10
    before = path.read_bytes()
    writer.validate = lambda: False
    with pytest.raises(APIError, match="config_invalid_after_write"):
        writer.save("companion_a", {"plugins": {"echo-tool": {"enabled": False}}})
    assert path.read_bytes() == before
