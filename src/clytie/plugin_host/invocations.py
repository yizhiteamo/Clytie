"""执行中的工具和服务。子任务可以继承上下文，但不能延长授权的寿命。"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import partial

from clytie.sdk.context import ServiceCall
from clytie.sdk.types import AssetRef, CurrentAppearance

_frame = ContextVar("plugin_invocation", default=None)


@dataclass
class GrantedAsset:
    ref: AssetRef
    read: Callable[[], Awaitable[bytes]]
    product: bool


@dataclass
class Frame:
    cid: str
    pid: str
    call: ServiceCall
    parent: Frame | None = None
    assets: dict[str, GrantedAsset] = field(default_factory=dict)
    active: bool = True

    def check(self):
        self.call.cancel.raise_if_cancelled()
        if not self.active:
            raise PermissionError("expired_invocation")
        if self.parent is not None:
            self.parent.check()

    def grant(self, asset):
        self.check()
        alias = secrets.token_hex(32)
        self.assets[alias] = asset
        return asset.ref.model_copy(update={"asset_id": alias})

    def lookup(self, asset_id):
        grant = self.assets.get(asset_id)
        if grant is not None:
            self.check()
        return grant

    def tool_result(self, result):
        if not result.assets:
            return result
        refs = []
        for ref in result.assets:
            grant = self.lookup(ref.asset_id)
            if grant is not None and not grant.product:
                raise PermissionError("restricted_asset")
            refs.append(grant.ref if grant is not None else ref)
        return result.model_copy(update={"assets": refs})


def current_frame(cid, pid):
    frame = _frame.get()
    return frame if frame and (frame.cid, frame.pid) == (cid, pid) else None


@contextmanager
def bind(frame):
    token = _frame.set(frame)
    try:
        yield
    finally:
        if frame is not None:
            frame.active = False
            frame.assets.clear()
        _frame.reset(token)


class AppearanceReader:
    def __init__(self, host, cid, pid):
        self.host, self.cid, self.pid = host, cid, pid

    async def current(self):
        frame = current_frame(self.cid, self.pid)
        if frame is None or frame.parent is not None:
            raise PermissionError("tool_execution_required")
        frame.check()
        agent = self.host.service_agents.get(self.cid)
        appearance = agent.appearance if agent is not None else None
        refs = []
        for entry in appearance.references if appearance else ():
            if entry.state == "current":
                read = partial(
                    asyncio.to_thread, self.host.asset_store.read_resolved, entry.resolved
                )
                refs.append(frame.grant(GrantedAsset(entry.asset, read, False)))
        return CurrentAppearance(
            description=appearance.description if appearance else "", references=refs
        )


async def transfer(value, source, target, assets, *, products=None):
    """Only complete serialized AssetRefs carry authority. Look up metadata at the source."""
    if isinstance(value, dict) and set(AssetRef.model_fields) <= value.keys():
        ref = AssetRef.model_validate(value)
        grant = source.lookup(ref.asset_id)
        if grant is None:
            actual = await assets.info(ref.asset_id)
            if ref != actual:
                raise ValueError("asset_metadata_mismatch")
            grant = GrantedAsset(actual, partial(assets.read, actual.asset_id), True)
        elif ref != grant.ref.model_copy(update={"asset_id": ref.asset_id}):
            raise ValueError("asset_metadata_mismatch")
        if products is not None and grant.product:
            products.append(grant.ref.asset_id)
        return target.grant(grant).model_dump(mode="json")
    if isinstance(value, dict):
        return {
            k: await transfer(v, source, target, assets, products=products)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [await transfer(v, source, target, assets, products=products) for v in value]
    return value


async def dispatch(
    host, runtime, registration, point, args, method, scene, conversation_id, caller
):
    cid, pid = runtime.companion_id, runtime.manifest.id
    invoke = partial(host._invoke, runtime, registration, point)
    if point == "tool" and method == "execute":
        _, inv, cancel = args
        call = ServiceCall(inv.trace_id, inv.actor, inv.conversation, cancel)
        frame = Frame(cid, pid, call)
        with bind(frame):
            result = await invoke(args, method, scene, conversation_id)
            return frame.tool_result(result)
    contextual = point == "service" and registration.declaration.get("context")
    scope_as = point == "service" and registration.declaration.get("scope_as")
    if point != "service" or not (contextual or scope_as or caller):
        with bind(None):
            return await invoke(args, method, scene, conversation_id)
    if caller is None or caller.cid != cid:
        return {"error": "denied"}
    caller.check()
    agent = host.service_agents.get(cid)
    call = caller.call
    service = Frame(cid, pid, call, parent=caller)
    caller_assets = host.runtimes[cid, caller.pid].ctx.assets
    name, payload = args
    products = []
    usage = {}
    status = "error"
    try:
        if scope_as and (agent is None or not agent.service_allowed(call, scope_as)):
            status = "denied"
            return {"error": "denied"}
        try:
            payload = await transfer(payload, caller, service, caller_assets)
        except (ValueError, OSError, KeyError):
            status = "denied"
            return {"error": "denied"}
        with bind(service):
            service.check()
            params = (name, payload, call) if contextual else (name, payload)
            result = await invoke(
                params, method, call.conversation.scene, call.conversation.conversation_id
            )
            reported = result.get("usage")
            if isinstance(reported, dict):
                usage = {
                    key: reported[key]
                    for key in ("input_tokens", "output_tokens", "total_tokens")
                    if type(reported.get(key)) is int and reported[key] >= 0
                }
            service.check()
            try:
                result = await transfer(
                    result, service, caller, runtime.ctx.assets, products=products
                )
            except (ValueError, OSError, KeyError) as exc:
                raise host.failure_type("invalid_service_asset") from exc
            status = "error" if "error" in result else "ok"
            return result
    except asyncio.CancelledError:
        status = "cancelled"
        raise
    finally:
        if agent is not None:
            await agent.audit_service(
                call.trace_id, pid, registration.key, name, status, products, usage
            )
