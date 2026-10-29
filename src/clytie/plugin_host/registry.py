"""从清单装配扩展点；实现对象只在宿主内部保存。"""

import inspect
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter

from clytie.sdk.behavior import AttentionDirective, BeforeAttentionDirective
from clytie.sdk.types import (
    QUOTA_UNKNOWN,
    ChannelFeatures,
    ContextSection,
    DeliveryIntent,
    Denial,
    EventEnvelope,
    MemoryHit,
    MemoryRecord,
    ModelFeatures,
    ModelTurn,
    Plan,
    Receipt,
    ReplyDraft,
    ToolResult,
)


@dataclass(frozen=True)
class Registration:
    point: str
    key: str
    declaration: Any
    implementation: Any
    priority: int
    plugin_id: str


FACTORIES = {
    "channel": {
        "connect": True,
        "close": True,
        "events": False,
        "send": True,
        "react": True,
        "features": False,
    },
    "model": {"generate": True, "features": False, "quota": True},
    "embedder": {"embed": True},
    "tool": {"idempotency_key": False, "prepare": True, "execute": True},
    "memory.retriever": {"search": True},
    "memory.reranker": {"rerank": True},
    "memory.importer": {"detect": False, "read": False},
    "memory.consolidator": {"run": True},
    "notifier": {"notify": True},
}


def assemble(runtime, *, role=None, primary=True):
    manifest, plugin = runtime.manifest, runtime.plugin
    contributions = manifest.data["contributes"]
    registrations = []

    def add(point, key, declaration, factory=False):
        member = getattr(plugin, declaration["factory" if factory else "handler"])
        if not callable(member):
            raise TypeError("declared plugin member is not callable")
        if factory:
            implementation = member()
            if inspect.isawaitable(implementation):
                if inspect.iscoroutine(implementation):
                    implementation.close()
                raise TypeError("plugin factories must be synchronous")
            for name, asynchronous in FACTORIES[point].items():
                function = getattr(implementation, name, None)
                if not callable(function) or (
                    asynchronous and not inspect.iscoroutinefunction(function)
                ):
                    raise TypeError("factory object does not implement its SDK protocol")
            if point in {"model", "channel"}:
                cls = ModelFeatures if point == "model" else ChannelFeatures
                if not isinstance(implementation.features(), cls):
                    raise TypeError("features must return its SDK type")
            if point == "embedder" and implementation.dimension != declaration["dimension"]:
                raise ValueError("embedder dimension does not match manifest")
        else:
            if not inspect.iscoroutinefunction(member):
                raise TypeError("plugin handlers must be asynchronous")
            implementation = member
        registrations.append(
            Registration(point, key, declaration, implementation, runtime.priority, manifest.id)
        )

    for point in ("channel", "model", "embedder", "notifier"):
        if point not in contributions:
            continue
        if point in {"channel", "model"} and role is not None and role != point:
            continue
        if point not in {"channel", "model"} and not primary:
            continue
        add(
            point,
            runtime.account_ref if point == "channel" and runtime.account_ref else point,
            contributions[point],
            True,
        )
    if not primary:
        return tuple(registrations)
    for declaration in contributions.get("tools", ()):
        add("tool", declaration["name"], declaration, True)
    for name, declaration in contributions.get("memory", {}).items():
        if not name.startswith("x-"):
            add("memory." + name, name, declaration, True)
    for phase, declaration in contributions.get("behavior", {}).items():
        if not phase.startswith("x-"):
            add("behavior." + phase, phase, declaration)
    if "context_section" in contributions:
        add("context_section", "context_section", contributions["context_section"])
    for field, point, key in (
        ("schedule", "schedule", "id"),
        ("services", "service", "name"),
        ("cli_commands", "cli_command", "name"),
    ):
        for declaration in contributions.get(field, ()):
            add(point, declaration[key], declaration)
    events = contributions.get("events", {})
    if events.get("subscribe"):
        add("event_subscriber", "events", events)
    for field, point, key in (
        ("api_routes", "api_route", "path"),
        ("webui_panels", "webui_panel", "id"),
    ):
        for declaration in contributions.get(field, ()):
            if field == "api_routes":
                member = getattr(plugin, declaration["handler"])
                if not inspect.iscoroutinefunction(member):
                    raise TypeError("API route handler must be asynchronous")
            registrations.append(
                Registration(
                    point, declaration[key], declaration, None, runtime.priority, manifest.id
                )
            )
    # config 是声明式扩展点，不执行代码。
    registrations.append(
        Registration(
            "config", "config", manifest.data["config"], None, runtime.priority, manifest.id
        )
    )
    keys = [(r.point, r.key) for r in registrations]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate contribution identifier")
    return tuple(registrations)


def validate_result(point, method, result):
    models = {
        ("model", "generate"): ModelTurn,
        ("channel", "send"): Receipt,
        ("channel", "react"): Receipt,
        ("notifier", "notify"): Receipt,
        ("channel", "features"): ChannelFeatures,
        ("model", "features"): ModelFeatures,
        ("tool", "execute"): ToolResult,
    }
    if (point, method) in models:
        cls = models[point, method]
        if not isinstance(result, cls):
            raise TypeError("plugin returned an invalid SDK type")
    if point == "tool" and method == "prepare" and not isinstance(result, (Plan, Denial)):
        raise TypeError("prepare must return Plan or Denial")
    if point == "tool" and method == "idempotency_key" and not isinstance(result, str):
        raise TypeError("idempotency key must be a string")
    if point == "memory.importer" and method == "detect" and not isinstance(result, bool):
        raise TypeError("detect must return bool")
    if point == "model" and method == "quota":
        from clytie.sdk.types import QuotaSnapshot

        if result != QUOTA_UNKNOWN and not isinstance(result, QuotaSnapshot):
            raise TypeError("quota must be a snapshot or QUOTA_UNKNOWN")
    if point in {"memory.retriever", "memory.consolidator"}:
        cls = MemoryHit if point == "memory.retriever" else MemoryRecord
        if not isinstance(result, list) or not all(isinstance(r, cls) for r in result):
            raise TypeError("invalid memory extension result")
    if point == "channel" and method == "events":
        if not all(isinstance(item, EventEnvelope) for item in result):
            raise TypeError("invalid inbound event")
    if point == "memory.importer" and method == "read":
        from clytie.sdk.types import ImportCandidate

        if not isinstance(result, list) or not all(isinstance(r, ImportCandidate) for r in result):
            raise TypeError("invalid importer record")
    if point == "memory.reranker":
        if not isinstance(result, list) or not all(
            isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score)
            for score in result
        ):
            raise TypeError("invalid reranker scores")
    if point == "service":
        if not isinstance(result, Mapping):
            raise TypeError("service must return a JSON object")
        json.dumps(dict(result), allow_nan=False)
    if point == "embedder":
        if not isinstance(result, list) or not all(
            isinstance(row, list) and all(isinstance(x, float) and math.isfinite(x) for x in row)
            for row in result
        ):
            raise TypeError("invalid embedding vectors")
    if point == "behavior.before_send" and result is not None:
        if not isinstance(result, list) or not all(isinstance(r, DeliveryIntent) for r in result):
            raise TypeError("before_send must return delivery intents")
    if (
        point
        in {"behavior.message_received", "behavior.task_completed", "schedule", "event_subscriber"}
        and result is not None
    ):
        raise TypeError("observer must return None")
    if point == "cli_command" and (not isinstance(result, int) or isinstance(result, bool)):
        raise TypeError("CLI handler must return exit code")
    if point == "behavior.before_attention" and result is not None:
        result = TypeAdapter(BeforeAttentionDirective).validate_python(result)
    elif point == "behavior.attention" and result is not None:
        result = TypeAdapter(AttentionDirective).validate_python(result)
    elif (
        point == "behavior.after_generation"
        and result is not None
        and not isinstance(result, ReplyDraft)
    ):
        raise TypeError("after_generation must return ReplyDraft")
    elif (
        point == "context_section" and result is not None and not isinstance(result, ContextSection)
    ):
        raise TypeError("context contributor must return ContextSection")
    return result
