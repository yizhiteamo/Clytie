import hashlib
import importlib.util
from pathlib import Path

from clytie.sdk.types import AssetRef

spec = importlib.util.spec_from_file_location(
    "comic_references", Path(__file__).resolve().parents[1] / "references.py"
)
references = importlib.util.module_from_spec(spec)
spec.loader.exec_module(references)


def picture(label):
    return AssetRef(
        asset_id=hashlib.sha256(label.encode()).hexdigest(),
        media_type="image/png",
        size=1,
        origin="fictional",
    )


def test_previous_page_then_first_appearance_then_round_robin():
    a = [picture(f"a-{i}") for i in range(6)]
    b = [picture(f"b-{i}") for i in range(6)]
    previous = picture("previous")
    panels = [{"characters": ["b"]}, {"characters": ["a", "b"]}]
    chosen, missing = references.page_references(panels, {"a": a, "b": b}, previous)
    assert chosen == [previous, b[0], a[0], b[1], a[1], b[2], a[2], b[3]]
    assert missing == []


def test_first_page_has_eight_slots_and_reports_unillustrated_characters():
    art = {f"role-{i}": [picture(str(i))] for i in range(9)}
    roles = list(art)
    chosen, missing = references.page_references([{"characters": roles}], art, None)
    assert chosen == [art[role][0] for role in roles[:8]]
    assert missing == roles[8:]
