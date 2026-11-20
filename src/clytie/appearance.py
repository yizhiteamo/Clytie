"""Local management of one companion's reference list and its asset registrations."""

import base64
import hashlib
import json
from datetime import datetime
from io import BytesIO
from pathlib import Path

import yaml
from PIL import Image

from clytie.agent.vision import normalize_image
from clytie.assets.store import AssetStore
from clytie.backup.coordination import CaptureCoordinator, companion_coordinator
from clytie.plugin_host.snapshots import atomic_read, atomic_write, contained, identifier
from clytie.sdk.types import AssetRef

MAX_BYTES = 20 * 1024 * 1024


def parse_manifest(data: bytes) -> dict:
    """Only active references require assets. History contains earlier list versions."""
    try:
        doc = yaml.safe_load(data)
        if not isinstance(doc, dict) or set(doc) != {"references", "history"}:
            raise ValueError
        if not isinstance(doc["history"], list):
            raise ValueError
        refs = doc["references"]
        if not isinstance(refs, list) or len(refs) > 6:
            raise ValueError
        ids = set()
        for row in refs:
            if set(row) != {"asset", "label", "state", "width", "height", "imported_at"}:
                raise ValueError
            asset = AssetRef.model_validate(row["asset"])
            if (
                asset.origin != "appearance"
                or asset.media_type != "image/png"
                or asset.asset_id in ids
                or row["state"] not in ("current", "former")
                or not isinstance(row["label"], str)
                or not 1 <= len(row["label"]) <= 20
                or any(
                    type(row[k]) is not int or not 1 <= row[k] <= 2048 for k in ("width", "height")
                )
            ):
                raise ValueError
            datetime.fromisoformat(row["imported_at"])
            ids.add(asset.asset_id)
        if refs and not any(row["state"] == "current" for row in refs):
            raise ValueError
        return doc
    except (KeyError, TypeError, ValueError, yaml.YAMLError) as exc:
        raise ValueError("invalid_appearance_manifest") from exc


def decode_upload(content):
    if not isinstance(content, str):
        raise ValueError("invalid_image")
    if len(content) > 4 * ((MAX_BYTES + 2) // 3):
        raise ValueError("too_large")
    try:
        raw = base64.b64decode(content, validate=True)
        png = normalize_image(raw, MAX_BYTES, clean_png=True).data
        with Image.open(BytesIO(png), formats=["PNG"]) as image:
            return png, image.size
    except AssertionError:
        # A violated encoder invariant is an internal error, not a rejected image.
        raise
    except Image.DecompressionBombError as exc:
        raise ValueError("too_many_pixels") from exc
    except ValueError as exc:
        if exc.args in (("too_large",), ("too_many_pixels",)):
            raise
        raise ValueError("invalid_image") from exc
    except (OSError, SyntaxError, EOFError) as exc:
        raise ValueError("invalid_image") from exc


class AppearanceFiles:
    def __init__(self, home, cid, clock):
        self.home, self.cid, self.clock = Path(home), identifier(cid), clock
        self.persona = self.home / "companions" / self.cid / "persona"
        self.store = AssetStore(self.home, clock)

    def path(self, filename):
        return contained(self.home, self.persona / filename)

    def edit_lock(self):
        return CaptureCoordinator(
            contained(self.home, self.persona.parent / "appearance-edit.lock"), self.clock
        )

    @property
    def check_path(self):
        return contained(self.home, self.persona.parent / "state" / "appearance-check.json")

    def manifest(self):
        path = self.path("appearance.yaml")
        if not path.exists():
            return {"references": [], "history": []}
        return parse_manifest(atomic_read(path))

    def show(self):
        refs = self.manifest()["references"]
        result = {
            "description_present": self.path("appearance.md").is_file(),
            "references": [
                {"number": n, **{k: row[k] for k in ("label", "state", "width", "height")}}
                for n, row in enumerate(refs, 1)
            ],
            "self_check": None,
        }
        if self.check_path.is_file():
            result["self_check"] = json.loads(atomic_read(self.check_path)).get("result")
        return result

    def change(self, action, data):
        fields = {
            "add": {"content", "label", "state"},
            "edit": {"number", "label", "state"},
            "remove": {"number"},
        }[action]
        if data.keys() - fields or (action == "edit" and len(data) < 2):
            raise ValueError("invalid_request")
        if action == "add" or "label" in data:
            label = data.get("label")
            if not isinstance(label, str) or not 1 <= len(label) <= 20:
                raise ValueError("invalid_appearance_label")
        state = data.get("state", "current")
        if state not in ("current", "former"):
            raise ValueError("invalid_appearance_state")
        if action == "add":
            png, (width, height) = decode_upload(data.get("content"))
            asset_id = hashlib.sha256(png).hexdigest()
        # Keep edit ordinals stable while staging the object outside the snapshot boundary.
        with self.edit_lock().commit():
            document = self.manifest()
            refs = list(document["references"])
            if action == "add":
                if any(row["asset"]["asset_id"] == asset_id for row in refs):
                    raise ValueError("duplicate_appearance")
                if len(refs) >= 6:
                    raise ValueError("appearance_limit")
                if not refs and state == "former":
                    raise ValueError("current_appearance_required")
                resolved, old = self.store._stage_put(
                    self.cid, png, "image/png", "appearance", appearance_only=True
                )
                ref = resolved.registration.ref().model_copy(
                    update={"origin": "appearance", "media_type": "image/png"}
                )
                refs.append(
                    {
                        "asset": ref.model_dump(mode="json"),
                        "label": label,
                        "state": state,
                        "imported_at": self.clock.now().isoformat(),
                        "width": width,
                        "height": height,
                    }
                )
            else:
                number = data.get("number")
                if type(number) is not int or not 1 <= number <= len(refs):
                    raise ValueError("invalid_reference_number")
                index = number - 1
                if action == "remove":
                    removed = refs.pop(index)
                else:
                    refs[index] = {
                        **refs[index],
                        **{k: data[k] for k in ("label", "state") if k in data},
                    }
                if refs and not any(row["state"] == "current" for row in refs):
                    raise ValueError("current_appearance_required")
            document["history"].append(
                {"at": self.clock.now().isoformat(), "references": document["references"]}
            )
            document["references"] = refs
            output = yaml.safe_dump(document, allow_unicode=True).encode()
            with companion_coordinator(self.home, self.cid, self.clock).commit():
                if action == "add":
                    self.store._register(resolved, old)
                atomic_write(self.path("appearance.yaml"), output)
                if action == "remove":
                    self.store.release_appearance(self.cid, removed["asset"]["asset_id"])
        return {"saved": True, "restart_required": True, "message": "重启伴侣后生效"}
