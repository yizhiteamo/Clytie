"""认证清单的凭据缺项证明；绝不修改普通 load_home 的有效性。"""

import copy
import hashlib
import json
from pathlib import Path

from clytie.config import _Reader, load_home, repository_root, thaw
from clytie.config.credentials import credential_pointers
from clytie.plugin_host.snapshots import contained, identifier


def schema_hash(schema):
    return hashlib.sha256(
        json.dumps(thaw(schema), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def pointer(parts):
    return "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in parts)


def declared(schema, value, parts=()):
    """Only proven schema declarations; unsupported positions fail closed."""
    for relative in credential_pointers(schema, value):
        yield pointer((*parts, *relative))


def document_exclusions(
    home, file, document, *, document_kind, repo_root=None, validate_partial_configs=True
):
    """Resolve declarations for one current or historical configuration document.

    The file identifies the in-memory copy, not a path to read. Historical callers
    apply redaction to that copy without treating its proof as current credentials.
    """
    home = Path(home).resolve()
    root = Path(repo_root or repository_root())
    reader = _Reader(root)
    if document_kind not in {"instance", "companion"} or not isinstance(document, dict):
        raise ValueError("opaque_credential_config")
    schema = json.loads((root / f"schemas/{document_kind}.schema.json").read_bytes())
    reader.validate(document, schema, home / file)
    if reader.errors:
        raise ValueError("opaque_credential_config")
    result = []
    for group in ("accounts", "credentials") if document_kind == "instance" else ():
        references = document.get(group, {})
        for name, reference in references.items() if isinstance(references, dict) else ():
            if isinstance(reference, dict) and "secret" in reference:
                result.append(
                    dict(
                        file=file,
                        pointer=pointer((group, name, "secret")),
                        kind="file_reference",
                        schema_sha256=schema_hash(schema),
                    )
                )

    def plugin_config(file, prefix, pid, value):
        try:
            identifier(pid)
        except (ValueError, TypeError):
            raise ValueError("opaque_credential_config") from None
        candidates = [
            root / "plugins" / pid / "plugin.json",
            home / "plugins" / pid / "plugin.json",
        ]
        available = [p for p in candidates if p.is_file()]
        if len(available) != 1:
            raise ValueError("opaque_credential_config")
        manifest = reader.read(contained(available[0].parent, available[0]))
        if not isinstance(manifest, dict) or not isinstance(manifest.get("config"), dict):
            raise ValueError("opaque_credential_config")
        schema = manifest["config"]
        declarations = list(declared(schema, value))
        if validate_partial_configs:
            start = len(reader.errors)
            reader.validate(value, schema, home / file)
            # A raw layer can depend on required fields supplied by another
            # layer. Unknown fields and every other validation error are opaque.
            if any(error.code != "schema_required" for error in reader.errors[start:]):
                raise ValueError("opaque_credential_config")
        for relative in declarations:
            result.append(
                dict(
                    file=file,
                    pointer=pointer(prefix) + relative,
                    kind="inline",
                    schema_sha256=schema_hash(schema),
                )
            )

    def plugin_settings(file, document):
        settings_by_id = document.get("plugins", {})
        if not isinstance(settings_by_id, dict):
            return
        for pid, settings in settings_by_id.items():
            if isinstance(settings, dict):
                plugin_config(file, ("plugins", pid, "config"), pid, settings.get("config", {}))

    plugin_settings(file, document)
    if document_kind == "companion":
        for keys in (("model",), ("memory", "recollect", "model")):
            value = document
            for key in keys:
                value = value.get(key, {}) if isinstance(value, dict) else {}
            if isinstance(value, dict) and value:
                plugin_config(file, (*keys, "config"), value.get("plugin"), value.get("config", {}))
        channels = document.get("channels", [])
        for i, value in enumerate(channels if isinstance(channels, list) else []):
            if isinstance(value, dict):
                plugin_config(
                    file, ("channels", i, "config"), value.get("plugin"), value.get("config", {})
                )
    return result


def credential_exclusions(home, *, repo_root=None):
    """Schema declarations only. Caller authenticates this list as part of the package."""
    home = Path(home).resolve()
    root = Path(repo_root or repository_root())
    reader = _Reader(root)
    instance = reader.read(contained(home, home / "clytie.local.yaml"))
    result = document_exclusions(
        home,
        "clytie.local.yaml",
        instance,
        document_kind="instance",
        repo_root=root,
        validate_partial_configs=False,
    )
    for cid in instance.get("companions", []):
        identifier(cid)
        file = f"companions/{cid}/companion.yaml"
        document = reader.read(contained(home, home / file))
        result.extend(
            document_exclusions(
                home,
                file,
                document,
                document_kind="companion",
                repo_root=root,
                validate_partial_configs=False,
            )
        )
    return result


def redact_document(document, file, declarations):
    """Remove only declared inline values in an in-memory backup copy.

    File references remain references; their secret files are excluded by the
    collector. No replacement values or mutations of the live configuration.
    """
    result = copy.deepcopy(document)
    for entry in declarations:
        if entry["file"] != file or entry["kind"] != "inline":
            continue
        parts = [p.replace("~1", "/").replace("~0", "~") for p in entry["pointer"].split("/")[1:]]
        if not parts:
            raise ValueError("opaque_credential_config")
        parent = result
        for part in parts[:-1]:
            if isinstance(parent, dict):
                parent = parent.get(part)
            elif isinstance(parent, list) and part.isdecimal() and int(part) < len(parent):
                parent = parent[int(part)]
            else:
                parent = None
                break
        if isinstance(parent, dict):
            parent.pop(parts[-1], None)
        elif isinstance(parent, list):
            # Removing an array entry changes other identities; no implicit null.
            raise ValueError("opaque_credential_config")
    return result


def validate_restored(home, authenticated_exclusions, *, repo_root=None):
    """Called only after package authentication; never a business startup bypass."""
    home = Path(home).resolve()
    report = load_home(home, repo_root=repo_root)
    declaration_invalid = False
    try:
        declarations = credential_exclusions(home, repo_root=repo_root)
    except (ValueError, OSError, TypeError):
        declarations = []
        declaration_invalid = True
    known = {(d["file"], d["pointer"], d["kind"], d["schema_sha256"]) for d in declarations}
    permitted = set()
    proof_invalid = declaration_invalid or not isinstance(authenticated_exclusions, list)
    if not isinstance(authenticated_exclusions, list):
        authenticated_exclusions = []
    for d in authenticated_exclusions:
        if (
            not isinstance(d, dict)
            or set(d) != {"file", "pointer", "kind", "schema_sha256"}
            or not all(isinstance(v, str) for v in d.values())
        ):
            proof_invalid = True
            continue
        key = (d["file"], d["pointer"], d["kind"], d["schema_sha256"])
        if key not in known or key in permitted:
            proof_invalid = True
        else:
            permitted.add(key)

    def diagnostic(error):
        try:
            file = Path(error.file).resolve().relative_to(home).as_posix()
        except ValueError:
            file = "plugin_schema"
        return dict(file=file, pointer=error.pointer, code=error.code or "config_invalid")

    errors = [diagnostic(e) for e in report.errors]
    missing = []
    remaining = []
    for e in errors:
        kind = {"credential_reference_missing": "file_reference", "schema_required": "inline"}.get(
            e["code"]
        )
        if kind and any(k[:3] == (e["file"], e["pointer"], kind) for k in permitted):
            missing.append({"file": e["file"], "pointer": e["pointer"], "kind": kind})
        else:
            remaining.append(e)
    return dict(
        data_valid=not proof_invalid and not remaining and report.exists,
        credential_exclusions=missing,
        exclusion_proof_valid=not proof_invalid,
        configuration=dict(
            valid=report.valid,
            errors=errors,
            global_errors=[diagnostic(e) for e in report.global_errors],
            companions=[dict(id=c["id"], valid=c["valid"]) for c in report.companions],
        ),
    )
