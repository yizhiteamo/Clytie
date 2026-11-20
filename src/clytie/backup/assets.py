"""资产捕获与恢复映射；内容去重不合并授权，也不搜索未知文件。"""

import hashlib
import json
import os
import stat
from pathlib import Path

from clytie.assets import validate_registry
from clytie.plugin_host.snapshots import atomic_read, canonical, contained

from .io_client import heavy_io, io_read, io_write

REGISTRY = "shared/assets/registry.json"


def configuration(home, cid, repo_root, manifests, *, documents):
    from clytie.config import load_home
    from clytie.config.assets import companion_dependencies

    from .configuration import config_commit_lock
    from .credentials import schema_hash

    with config_commit_lock(home):

        def check_documents():
            if any(
                atomic_read(contained(home, Path(home) / name)) != raw
                for name, raw in documents.items()
            ):
                raise ValueError("snapshot_inconsistent")

        check_documents()
        report = load_home(home, repo_root=repo_root)
        check_documents()
    if not report.valid:
        raise ValueError("asset_configuration_invalid")
    return [
        {**row, "schema_sha256": schema_hash(manifests[row["plugin_id"]].data["config"])}
        for row in companion_dependencies(report, cid, manifests)
    ]


def validate_configuration(root, envelope, permissions, credentials, repo_root):
    from clytie.config import load_home
    from clytie.config.assets import AssetDeclarationError, companion_dependencies
    from clytie.plugin_host.manifests import discover

    from .credentials import schema_hash

    report = load_home(root, repo_root=repo_root)
    manifests = discover(Path(repo_root) / "plugins", Path(root) / "plugins", repo_root=repo_root)
    for companion in envelope["manifest"]["companions"]:
        cid = companion["id"]
        recorded = companion.get("configuration_assets", [])
        configs = report.plugin_configs[cid]
        config = configs["config"]
        units = {}
        for pid, value in configs["plugins"].items():
            units["plugins", pid] = (f"/plugins/{pid}/config", value)
        for selection, unit, prefix in (
            (config.get("model"), "model", "/model/config"),
            (
                config.get("memory", {}).get("recollect", {}).get("model"),
                "recollect_model",
                "/memory/recollect/model/config",
            ),
        ):
            if selection:
                units[unit, selection["plugin"]] = (prefix, configs[unit])
        for index, selection in enumerate(config.get("channels", ())):
            units[f"channel:{index}", selection["plugin"]] = (
                f"/channels/{index}/config",
                configs["channels"][index],
            )
        for row in recorded:
            if set(row) != {"pointer", "asset_id", "plugin_id", "unit", "schema_sha256"}:
                raise ValueError("restore_asset_invalid")
            pid = row["plugin_id"]
            if row["schema_sha256"] != schema_hash(manifests[pid].data["config"]):
                raise ValueError("restore_asset_invalid")
            prefix, value = units[row["unit"], pid]
            if not row["pointer"].startswith(prefix + "/"):
                raise ValueError("restore_asset_invalid")
            for component in row["pointer"][len(prefix) + 1 :].split("/"):
                component = component.replace("~1", "/").replace("~0", "~")
                value = (
                    value[int(component)] if isinstance(value, (list, tuple)) else value[component]
                )
            if value != row["asset_id"] or value not in permissions.get(cid, set()):
                raise ValueError("restore_asset_unauthorized")
        try:
            actual = companion_dependencies(report, cid, manifests)
        except AssetDeclarationError as exc:
            # Credential absence was already precisely authenticated and validated.
            # Keep the original authenticated dependency proof and verify its field
            # values/schema above; never guess a new branch using fake credentials.
            if (
                exc.code != "asset_configuration_invalid"
                or not credentials["credential_exclusions"]
            ):
                raise
        else:
            expected = [
                {key: value for key, value in row.items() if key != "schema_sha256"}
                for row in recorded
            ]
            if sorted(map(canonical, actual)) != sorted(map(canonical, expected)):
                raise ValueError("restore_asset_invalid")


def boundary(store, cid):
    snapshot = store.snapshot()
    resolved = {}
    for row in snapshot.registrations:
        if cid in (row.companion_id, *row.shared_with):
            try:
                resolved[row.asset_id] = store.resolve(snapshot, cid, row.asset_id)
            except (KeyError, ValueError, OSError):
                resolved[row.asset_id] = None
    return snapshot, resolved


@heavy_io
def capture(store, cid, fixed, ids, root, *, declarations=(), appearance=None, check=None):
    files, assets, missing = [], [], []
    declared = {}
    for ref in declarations:
        if ref.asset_id in declared and ref != declared[ref.asset_id]:
            raise ValueError("asset_reference_invalid")
        declared[ref.asset_id] = ref
    appearance = appearance or {}
    for asset_id in sorted(set(ids) | set(declared) | set(appearance)):
        resolved = fixed.get(asset_id)
        try:
            if resolved is None:
                raise ValueError("asset_source_unverified")
            row = resolved.registration
            if asset_id in appearance and (
                row.companion_id != cid or row.size != appearance[asset_id]
            ):
                raise ValueError("asset_source_unverified")
            if asset_id in declared and row.ref() != declared[asset_id]:
                raise ValueError("asset_metadata_mismatch")
            target = contained(root, Path(root) / row.path)
            target.parent.mkdir(parents=True, exist_ok=True)
            store.copy_resolved(resolved, target, check=check, reader=io_read, writer=io_write)
            files.append(dict(path=row.path, size=row.size, sha256=row.asset_id))
            assets.append(
                dict(
                    asset_id=row.asset_id,
                    size=row.size,
                    sha256=row.asset_id,
                    path=row.path,
                    registrations=[row.as_dict()],
                    references=[cid],
                )
            )
        except (KeyError, ValueError, OSError):
            if check:
                check()
            missing.append(
                dict(component=f"assets:{cid}", reason="asset_source_unverified", asset_id=asset_id)
            )
    return files, assets, missing


def consolidate(root, result, epoch):
    """仅处理已成功捕获的明确文件；重复内容删除单个受控暂存副本。"""
    by_hash, rows, removed = {}, {}, set()
    for asset in result["assets"]:
        asset_id = asset["asset_id"]
        current = by_hash.setdefault(asset_id, {**asset, "registrations": [], "references": []})
        if any(current[key] != asset[key] for key in ("size", "sha256")):
            raise ValueError("asset_metadata_mismatch")
        if asset["path"] != current["path"]:
            removed.add(asset["path"])
        for row in asset["registrations"]:
            key = row["companion_id"], row["asset_id"]
            if key in rows and rows[key] != row:
                raise ValueError("asset_registration_changed")
            rows[key] = row
            if row not in current["registrations"]:
                current["registrations"].append(row)
        current["references"] = sorted(set(current["references"]) | set(asset["references"]))
    if not by_hash:
        return
    registry = dict(
        format=1,
        revision=hashlib.sha256(canonical(list(rows.values()))).hexdigest()[:32],
        registrations=list(rows.values()),
    )
    validate_registry(registry)
    for name in sorted(removed):
        contained(root, Path(root) / name).unlink()
    result["files"] = [entry for entry in result["files"] if entry["path"] not in removed]
    for group in result["consistency_groups"]:
        group["members"] = [name for name in group["members"] if name not in removed]
        group["sha256"] = {
            name: value for name, value in group["sha256"].items() if name not in removed
        }
    path = contained(root, Path(root) / REGISTRY)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical(registry)
    with path.open("xb") as stream:
        stream.write(data)
    digest = hashlib.sha256(data).hexdigest()
    result["files"].append(dict(path=REGISTRY, size=len(data), sha256=digest))
    result["consistency_groups"].append(
        dict(epoch=epoch, members=[REGISTRY], sha256={REGISTRY: digest})
    )
    result["assets"] = list(by_hash.values())


def layout(root, envelope):
    """包清单映射和登记必须完全一致，随后调用方分别校验所有内容成员。"""
    manifest = envelope["manifest"]
    asset_members = {
        row["path"]
        for row in envelope["files"]
        if row["path"].startswith("shared/assets/")
        or (
            row["path"].startswith("companions/")
            and len(row["path"].split("/")) > 3
            and row["path"].split("/")[2] == "assets"
        )
    }
    if manifest["schema_version"] == 1:
        if manifest["assets"] or asset_members:
            raise ValueError("restore_asset_unsupported")
        return {}, {}
    if manifest["schema_version"] != 2:
        raise ValueError("restore_asset_unsupported")
    if asset_members != {REGISTRY, *(row["path"] for row in manifest["assets"])}:
        raise ValueError("restore_asset_invalid")
    registry = validate_registry(json.loads(contained(root, Path(root) / REGISTRY).read_bytes()))
    listed = {}
    permissions, targets = {}, {}
    members = {row["path"]: row for row in envelope["files"]}
    for asset in manifest["assets"]:
        asset_id = asset["asset_id"]
        if asset_id in listed or asset["sha256"] != asset_id:
            raise ValueError("restore_asset_invalid")
        if members.get(asset["path"]) != dict(
            path=asset["path"], size=asset["size"], sha256=asset_id
        ):
            raise ValueError("restore_asset_missing")
        listed[asset_id] = asset
        if asset["path"] not in {row["path"] for row in asset["registrations"]}:
            raise ValueError("restore_asset_invalid")
        for raw in asset["registrations"]:
            if raw["asset_id"] != asset_id or raw["size"] != asset["size"]:
                raise ValueError("restore_asset_invalid")
            key = raw["companion_id"], asset_id
            if key in targets:
                raise ValueError("restore_asset_invalid")
            targets[key] = raw
            for cid in (raw["companion_id"], *raw["shared_with"]):
                permissions.setdefault(cid, set()).add(asset_id)
        if not set(asset["references"]) <= {
            cid for cid, ids in permissions.items() if asset_id in ids
        }:
            raise ValueError("restore_asset_unauthorized")
    if sorted(canonical(row.as_dict()) for row in registry.registrations) != sorted(
        canonical(row) for row in targets.values()
    ):
        raise ValueError("restore_asset_invalid")
    return permissions, {row["path"]: listed[row["asset_id"]] for row in targets.values()}


def restore_size(envelope):
    members = {row["path"] for row in envelope["files"]}
    extra = {}
    for asset in envelope["manifest"]["assets"]:
        for row in asset.get("registrations", ()):
            if row["path"] not in members:
                extra[row["path"]] = row["size"]
    return sum(row["size"] for row in envelope["files"]) + sum(extra.values())


@heavy_io
def materialize(root, envelope, *, check=None):
    _, targets = layout(root, envelope)
    for name, asset in targets.items():
        path = contained(root, Path(root) / name)
        source = contained(root, Path(root) / asset["path"])
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as incoming, path.open("xb") as outgoing:
                while chunk := io_read(incoming, 1024 * 1024):
                    if check:
                        check()
                    io_write(outgoing, chunk)
                outgoing.flush()
                os.fsync(outgoing.fileno())
        if path.stat().st_size != asset["size"]:
            raise ValueError("restore_asset_invalid")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while chunk := io_read(stream, 1024 * 1024):
                if check:
                    check()
                digest.update(chunk)
        if digest.hexdigest() != asset["asset_id"]:
            raise ValueError("restore_asset_invalid")
        path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return set(targets)
