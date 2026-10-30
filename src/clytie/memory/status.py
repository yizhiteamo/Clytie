"""只读记忆状态，不启动插件，不打印任何记忆内容。"""

import json
from pathlib import Path

from clytie.config import thaw
from clytie.plugin_host.snapshots import contained

from .context import MemoryContext
from .store import MemoryStore


def notices(report, *, clock):
    result = []
    home = Path(report.home)
    for companion in report.companions:
        cid = companion["id"]
        config = report.plugin_configs.get(cid, {}).get("config", {})
        path = home / "companions" / cid / "memory" / "memory.sqlite3"
        if not path.is_file():
            continue
        store = None
        try:
            contained(home, path)
            store = MemoryStore(
                path,
                clock=clock,
                config=thaw(config),
                instance=thaw(report.instance),
                readonly=True,
            )
            failures = store.meta("recollect_semantic_failures", 0)
            if failures:
                result.append(
                    dict(
                        companion_id=cid,
                        code="recollect_semantic_unavailable",
                        count=failures,
                        message=f"有 {failures} 次回想没能做语义比对",
                    )
                )
            if store.has_table("recollect_progress"):
                for row in store.db.execute(
                    "SELECT * FROM recollect_progress WHERE conversation_id != ?", ("*",)
                ):
                    progress = json.loads(row["data"])
                    if row["conversation_id"] == "!state":
                        if progress.get("skipped"):
                            result.append(
                                dict(
                                    companion_id=cid,
                                    code="recollect_skipped",
                                    count=progress["skipped"],
                                    message=f"停用期间有 {progress['skipped']} 段对话没有回想",
                                )
                            )
                        continue
                    result.append(
                        dict(
                            companion_id=cid,
                            code="recollect_progress",
                            conversation_id=row["conversation_id"],
                            **{
                                key: progress.get(key)
                                for key in ("at", "written", "dropped", "failures")
                            },
                        )
                    )
            active = [r for r in store.all_records() if not r.retracted]
            from .origins import binding, resolve

            sourced = [
                r
                for r in active
                if r.origin_conversation_id is not None
                and not (r.kind == "profile" and "core:profile" in r.tags)
            ]
            missing = sum(resolve(store, r) is None for r in sourced)
            restored = sum(
                binding(store, r) is not None and resolve(store, r) is not None for r in sourced
            )
            if restored:
                result.append(
                    dict(
                        companion_id=cid,
                        code="memory_origins_resolved",
                        count=restored,
                        message=f"有 {restored} 条记录的来源会话已对应",
                    )
                )
            if missing:
                result.append(dict(companion_id=cid, code="memory_missing_origins", count=missing))
            if not config.get("memory", {}).get("embedder") or store.meta(
                "embedder_unavailable", False
            ):
                result.append(
                    dict(
                        companion_id=cid,
                        code="memory_keyword_only",
                        message="向量模型不可用，仅使用关键词",
                    )
                )
            progress = store.meta("embedding_progress")
            if progress and progress["complete"] < progress["total"]:
                result.append(dict(companion_id=cid, code="memory_embedding_progress", **progress))
            # 仅计算对内核心记录的展示预算，不把原文放进状态结果。
            core = sorted(
                (
                    r
                    for r in store.project("owner_private", None)
                    if r.kind not in {"profile", "jargon"}
                    and (
                        r.basis == "owner_confirmed"
                        or r.kind == "preference"
                        and r.basis == "owner_stated"
                    )
                ),
                key=lambda r: (r.updated_at, r.record_id),
                reverse=True,
            )
            memory = MemoryContext(store, log=None)
            session = memory.session("status")
            used, count = 0, 0
            for record in core:
                line = memory.label(record, session)
                if used + len(line) <= config.get("memory", {}).get("core_chars", 4000):
                    used += len(line)
                    count += 1
                else:
                    session.references.pop(f"[记忆{len(session.references)}]")
            if len(core) > count:
                result.append(
                    dict(companion_id=cid, code="memory_core_overflow", count=len(core) - count)
                )
        except Exception:
            result.append(dict(companion_id=cid, code="memory_state_unreadable"))
        finally:
            if store is not None:
                store.close()
    directory = home / "run" / "memory-temp"
    if directory.is_dir():
        try:
            contained(home, directory)
            for path in sorted(directory.iterdir()):
                contained(home, path)
                if path.is_file():
                    info = path.stat()
                    result.append(
                        dict(
                            code="memory_temporary_file_retained",
                            file=path.name,
                            bytes=info.st_size,
                            modified_at=info.st_mtime,
                        )
                    )
        except (OSError, ValueError):
            result.append(dict(code="memory_temporary_files_unreadable"))
    return result
