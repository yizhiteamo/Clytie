"""只从仓库官方清单生成确定的创意工坊索引。"""

import argparse
import json
from pathlib import Path

from clytie.config import thaw
from clytie.plugin_host.manifests import discover

ROOT = Path(__file__).resolve().parents[1]


def catalog(root=ROOT):
    records = []
    for manifest in discover(root / "plugins", repo_root=root).values():
        data = manifest.data
        records.append(
            {
                **{key: data[key] for key in ("id", "name", "version", "summary", "category")},
                "dependencies": thaw(data.get("dependencies", {})),
                "conflicts": thaw(data.get("conflicts", [])),
                "permissions": thaw(data["permissions"]),
                "release_notes": None,
                "source": {"kind": "official", "url": None},
                "signature": None,
            }
        )
    return (
        json.dumps(
            {"schema_version": 1, "plugins": records}, ensure_ascii=False, sort_keys=True, indent=2
        )
        + "\n"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    expected = catalog()
    path = ROOT / "plugins" / "catalog.json"
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != expected:
            print("catalog.json is stale; run python scripts/build_catalog.py")
            return 1
    else:
        path.write_text(expected, encoding="utf-8")
    print(
        "Official plugin catalog is current: 6 reference plugins"
        if len(json.loads(expected)["plugins"]) == 6
        else "Official plugin catalog is current"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
