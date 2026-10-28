import pytest

from clytie.config import load_home

from .test_config import make_home, write_yaml


@pytest.mark.parametrize(
    "problem",
    ["public_group", "stranger_private", "undeclared", "missing_members", "outsider", "valid"],
)
def test_private_scene_configuration_is_validated(tmp_path, problem):
    home, repo, instance, companion = make_home(tmp_path)
    scene = problem if problem in {"public_group", "stranger_private", "undeclared"} else "family"
    companion["memory"] = {"private_scenes": ["owner_private", scene]}
    if scene == "family":
        instance["scenes"].append(scene)
        companion["persona"]["scenes"][scene] = ["base"]
        instance["conversations"]["family_group"] = {
            "channel": "fake-channel",
            "platform_id": "1000000008",
            "kind": "group",
            "scene": scene,
        }
        if problem != "missing_members":
            instance["conversations"]["family_group"]["members_expected"] = ["owner", "companion_a"]
        if problem == "outsider":
            instance["people"]["outsider"] = {"role": "member", "accounts": []}
            instance["conversations"]["family_group"]["members_expected"].append("outsider")
    write_yaml(home / "clytie.local.yaml", instance)
    write_yaml(home / "companions/companion_a/companion.yaml", companion)
    report = load_home(home, repo_root=repo)
    assert report.valid == (problem == "valid"), report.to_dict()
    if problem != "valid":
        assert any(
            e.pointer.startswith("/memory/private_scenes/") and e.file.endswith("companion.yaml")
            for e in report.errors
        )
