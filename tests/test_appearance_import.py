"""Exercise reference imports, history and model-only access with fictional images."""

import base64
from io import BytesIO
from random import Random

import pytest
from PIL import Image, PngImagePlugin

from clytie.agent import vision
from clytie.appearance import MAX_BYTES, AppearanceFiles
from clytie.assets.store import model_image_access
from clytie.sdk.testing import MockClock
from clytie.sdk.types import AssetRef


@pytest.fixture
def files(tmp_path):
    return AppearanceFiles(tmp_path, "companion_a", MockClock())


def encode(size, fmt="PNG", *, seed=43, metadata=None):
    pixels = Random(seed).randbytes(size[0] * size[1] * 3)
    with Image.frombytes("RGB", size, pixels) as image, BytesIO() as output:
        image.save(output, format=fmt, pnginfo=metadata)
        return output.getvalue()


def add(files, data, **settings):
    return files.change(
        "add", {"content": base64.b64encode(data).decode(), "label": "portrait", **settings}
    )


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        ((3072, 3072), (2048, 2048)),
        ((180, 135), (180, 135)),
        ((2560, 1920), (2048, 1536)),
        ((1600, 3200), (1024, 2048)),
    ],
)
def test_reference_import_dimensions_and_attachment_budget(files, size, expected):
    add(files, encode(size, "JPEG"))
    (row,) = files.show()["references"]
    assert (row["width"], row["height"]) == expected
    ref = files.manifest()["references"][0]["asset"]
    snapshot = files.store.resolve(files.store.snapshot(), files.cid, ref["asset_id"])
    data = files.store.read_resolved(snapshot)
    assert len(data) == ref["size"] < MAX_BYTES
    with Image.open(BytesIO(data), formats=["PNG"]) as image:
        assert image.size == expected


def test_reference_size_invariant_failure_is_internal_and_writes_nothing(files, monkeypatch):
    monkeypatch.setattr(vision, "REFERENCE_ATTACHMENT_BYTES", 5)
    with pytest.raises(AssertionError, match="unexpected PNG size"):
        add(files, encode((8, 8)))
    assert not files.path("appearance.yaml").exists()
    assert not files.store.registry_path.exists()


def test_reference_deduplicates_normalized_pixels_and_checks_states_before_writing(files):
    data = encode((16, 16))
    with pytest.raises(ValueError, match="current_appearance_required"):
        add(files, data, state="former")
    assert not files.path("appearance.yaml").exists()
    assert not files.store.registry_path.exists()
    add(files, data)
    add(files, encode((8, 9)), state="former")
    before = files.path("appearance.yaml").read_bytes(), files.store.registry_path.read_bytes()
    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "fictional duplicate")
    for action, request, code in [
        (
            "add",
            {
                "content": base64.b64encode(encode((16, 16), metadata=info)).decode(),
                "label": "same pixels",
            },
            "duplicate_appearance",
        ),
        ("edit", {"number": 1, "state": "former"}, "current_appearance_required"),
        ("remove", {"number": 1}, "current_appearance_required"),
        ("add", {"label": "test", "content": "AAAA"}, "invalid_image"),
    ]:
        with pytest.raises(ValueError, match=code):
            files.change(action, request)
        assert (
            files.path("appearance.yaml").read_bytes(),
            files.store.registry_path.read_bytes(),
        ) == before
    files.change("remove", {"number": 2})
    files.change("remove", {"number": 1})
    manifest = files.manifest()
    assert manifest["references"] == []
    assert manifest["history"][-1]["references"][0]["label"] == "portrait"


def test_reference_limits_keep_files_intact(files, monkeypatch):
    for n in range(6):
        add(files, encode((16, 16), seed=n))
    before = files.path("appearance.yaml").read_bytes(), files.store.registry_path.read_bytes()
    objects = set((files.store.root / "objects").iterdir())
    for code, source in (
        ("appearance_limit", encode((24, 24))),
        ("too_large", bytes(MAX_BYTES + 1)),
    ):
        with pytest.raises(ValueError, match=code):
            add(files, source)
    monkeypatch.setattr(vision, "MAX_IMAGE_PIXELS", 16)
    with pytest.raises(ValueError, match="too_many_pixels"):
        add(files, encode((5, 5)))
    assert (
        files.path("appearance.yaml").read_bytes(),
        files.store.registry_path.read_bytes(),
    ) == before
    assert set((files.store.root / "objects").iterdir()) == objects


async def test_reference_read_requires_selected_model_and_running_profile(files):
    add(files, encode((16, 16)))
    ref = AssetRef.model_validate(files.manifest()["references"][0]["asset"])
    resolved = files.store.resolve(files.store.snapshot(), files.cid, ref.asset_id)
    pinned = {ref.asset_id: resolved}
    model = files.store.bind(files.cid, "model")
    tool = files.store.bind(files.cid, "tool")
    with pytest.raises(KeyError):
        await model.read(ref.asset_id)
    with model_image_access(files.cid, "model", [], appearance=pinned):
        with pytest.raises(KeyError):
            await model.read(ref.asset_id)
    files.change("remove", {"number": 1})
    # The running profile was loaded before removal. The next startup sees no reference.
    with model_image_access(files.cid, "model", [ref], appearance=pinned):
        assert await model.info(ref.asset_id) == ref
        assert await model.read(ref.asset_id) == files.store.read_resolved(resolved)
        with pytest.raises(KeyError):
            await tool.info(ref.asset_id)
    with pytest.raises(KeyError):
        await model.read(ref.asset_id)
    assert not files.store.snapshot().registrations


@pytest.mark.parametrize("inbound", [True, False])
async def test_removing_appearance_preserves_independent_source(files, inbound):
    add(files, encode((16, 16)))
    ref = AssetRef.model_validate(files.manifest()["references"][0]["asset"])
    data = files.store.read_resolved(
        files.store.resolve(files.store.snapshot(), files.cid, ref.asset_id)
    )
    other = await files.store.put(files.cid, data, "image/png", "independent", inbound_only=inbound)
    files.change("remove", {"number": 1})
    assets = files.store.bind(files.cid, "model")
    if inbound:
        with pytest.raises(KeyError):
            await assets.read(ref.asset_id)
    with model_image_access(files.cid, "model", [other]):
        assert await assets.read(ref.asset_id) == data


async def test_existing_product_keeps_its_own_metadata(files):
    original = encode((8, 8))
    png = vision.normalize_image(original, MAX_BYTES, clean_png=True).data
    product = await files.store.put(files.cid, png, "application/octet-stream", "fixture")
    add(files, original)
    (row,) = files.manifest()["references"]
    assert row["asset"]["asset_id"] == product.asset_id
    assert (row["asset"]["media_type"], row["asset"]["origin"]) == ("image/png", "appearance")
    assert await files.store.bind(files.cid, "tool").info(product.asset_id) == product
    files.change("remove", {"number": 1})
    assert await files.store.bind(files.cid, "tool").read(product.asset_id) == png
