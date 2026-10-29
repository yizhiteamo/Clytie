"""v0.2 上下文和读取接口的可复用离线契约检查。"""

from collections.abc import Mapping
from pathlib import Path

from clytie.sdk.cancellation import CancelToken
from clytie.sdk.context import RevisionConflict, ServiceUnavailable
from clytie.sdk.immutable import thaw
from clytie.sdk.types import (
    ApiResponse,
    AssetRef,
    HttpResponse,
    MemoryRecord,
    ModelTurn,
    ProcessResult,
)

from .contracts import assert_roundtrip
from .helpers import assert_task_cancellation, assert_token_cancellation


def rejects(error, operation):
    try:
        operation()
    except error:
        return
    raise AssertionError(f"expected {error}")


async def rejects_async(error, operation):
    try:
        await operation()
    except error:
        return
    raise AssertionError(f"expected {error.__name__}")


def assert_logger_contract(log):
    for name in ("debug", "info", "warning", "error"):
        method = getattr(log, name)
        assert method("contract.operation", count=1, flags=[True]) is None
        rejects((TypeError, ValueError), lambda: method("contract.operation", value=object()))


async def assert_storage_contract(storage):
    assert isinstance(storage.path, Path) and storage.path.is_dir()
    value = {"nested": ["fictional"]}
    await storage.set("contract.value", value)
    value["nested"].append("changed")
    assert await storage.get("contract.value") == {"nested": ["fictional"]}
    assert "contract.value" in await storage.keys("contract.")
    await storage.delete("contract.value")
    assert await storage.get("contract.value") is None
    await rejects_async(ValueError, lambda: storage.set("x" * 201, None))
    await rejects_async(ValueError, lambda: storage.set("contract.value", "x" * (1024 * 1024)))
    for operation in (
        lambda: storage.get("contract.value"),
        lambda: storage.set("contract.value", {}),
        lambda: storage.delete("contract.value"),
        storage.keys,
    ):
        await assert_task_cancellation(operation)


async def assert_events_contract(events, plugin_id):
    assert await events.publish(plugin_id + ".changed", {"ready": True}) is None
    await rejects_async(ValueError, lambda: events.publish("core.changed", {}))
    await rejects_async(ValueError, lambda: events.publish(plugin_id + "other.changed", {}))
    await assert_task_cancellation(lambda: events.publish(plugin_id + ".changed", {}))


async def assert_state_contract(state, snapshot, emitted, plugin_id):
    payload = {"ready": [True]}
    await state.set(payload)
    payload["ready"].append(False)
    assert thaw(snapshot().states[plugin_id]) == {"ready": [True]}
    assert emitted()[-1].name == "companion.state_changed"
    assert emitted()[-1].payload["plugin_id"] == plugin_id
    await assert_task_cancellation(lambda: state.set({}))


async def assert_assets_contract(assets, asset, content):
    assert isinstance(asset, AssetRef)
    assert await assets.info(asset.asset_id) == asset
    assert await assets.read(asset.asset_id) == content
    path = await assets.path(asset.asset_id)
    assert isinstance(path, Path) and path.is_file()
    assert path.read_bytes() == content
    assert path.stat().st_mode & 0o222 == 0
    assert not hasattr(assets, "put")
    for method in (assets.info, assets.read, assets.path):
        await assert_task_cancellation(lambda: method(asset.asset_id))


async def assert_asset_writer_contract(writer, content=b"fictional"):
    ref = await writer.put(content, "application/octet-stream", "handwritten")
    assert isinstance(ref, AssetRef) and ref.size == len(content)
    assert_roundtrip(ref)
    await assert_task_cancellation(
        lambda: writer.put(content, "application/octet-stream", "handwritten")
    )
    return ref


async def assert_candidate_reader_contract(reader, expected, *, hidden_ids=()):
    expected = list(expected)
    assert await reader.count() == len(expected)
    batches = [batch async for batch in reader.iter(batch=1)]
    assert all(len(batch) == 1 for batch in batches)
    assert [r for batch in batches for r in batch] == expected
    ids = [r.record_id for r in expected]
    assert await reader.get(ids + list(hidden_ids)) == expected
    assert await reader.get(list(hidden_ids)) == []
    if expected:
        first = expected[0]
        selected = [
            r
            async for batch in reader.iter(
                kinds=[first.kind], scope=first.scope, updated_after=first.updated_at, batch=2
            )
            for r in batch
        ]
        assert all(
            r.kind == first.kind
            and (first.scope is None or r.scope == first.scope)
            and r.updated_at > first.updated_at
            for r in selected
        )
    await assert_task_cancellation(reader.count)
    await assert_task_cancellation(lambda: reader.get(ids))

    async def iterate():
        return [batch async for batch in reader.iter()]

    await assert_task_cancellation(iterate)


async def assert_api_client_contract(api):
    result = await api.request("GET", "/api/v1/status", json=None, query={"mode": "fictional"})
    assert isinstance(result, ApiResponse)
    assert_roundtrip(result)
    await rejects_async(ValueError, lambda: api.request("GET", "/outside"))
    await assert_task_cancellation(lambda: api.request("GET", "/api/v1/status"))


async def assert_memory_view_contract(memory, *, record, hidden_id):
    results = await memory.search("")
    assert all(isinstance(r, MemoryRecord) and r.visibility != "private" for r in results)
    assert await memory.get(hidden_id) is None
    assert await memory.get(record.record_id) == record
    await assert_task_cancellation(lambda: memory.search(""))
    await assert_task_cancellation(lambda: memory.get(record.record_id))


async def assert_memory_insert_contract(memory, draft):
    assert not any(hasattr(memory, name) for name in ("search", "get", "revise", "retract"))
    record = await memory.write(draft)
    assert record.write_mode == "auto"
    await assert_task_cancellation(lambda: memory.write(draft))


async def assert_memory_writer_contract(memory, draft):
    record = await memory.write(draft)
    assert record.write_mode == "auto"
    updated = await memory.revise(
        record.record_id, record.revision, {"content": "revised fictional fact"}
    )
    assert updated.revision == record.revision + 1 and updated.content == "revised fictional fact"
    await rejects_async(
        RevisionConflict, lambda: memory.revise(record.record_id, record.revision, {})
    )
    await rejects_async(
        RevisionConflict, lambda: memory.retract(record.record_id, record.revision, "fictional")
    )
    await memory.retract(updated.record_id, updated.revision, "fictional")
    assert await memory.get(updated.record_id) is None
    for op in (
        lambda: memory.write(draft),
        lambda: memory.revise(updated.record_id, updated.revision, {}),
        lambda: memory.retract(updated.record_id, updated.revision, "fictional"),
    ):
        await assert_task_cancellation(op)


async def assert_service_proxy_contract(proxy, *, unavailable):
    assert isinstance(await proxy.call("get", {}), Mapping)
    await rejects_async(ServiceUnavailable, lambda: unavailable.call("get", {}))
    await assert_task_cancellation(lambda: proxy.call("get", {}))


def assert_services_contract(services, name, proxy):
    assert services.get(name) is proxy
    assert services.get("fictional.missing") is None


async def assert_peer_contract(peer):
    assert isinstance(await peer.send("companion_b", "fictional", {}), str)
    assert isinstance(thaw(await peer.request("companion_b", "fictional", {}, 5)), dict)
    await assert_task_cancellation(lambda: peer.send("companion_b", "fictional", {}))
    await assert_task_cancellation(lambda: peer.request("companion_b", "fictional", {}, 5))


async def assert_model_invoker_contract(model, request):
    assert isinstance(await model.generate(request, CancelToken(), purpose="contract"), ModelTurn)
    await assert_token_cancellation(
        lambda token: model.generate(request, token, purpose="contract")
    )
    await assert_task_cancellation(
        lambda: model.generate(request, CancelToken(), purpose="contract")
    )


async def assert_http_contract(http):
    result = await http.request(
        "GET", "https://example.invalid/", headers={}, body=None, timeout_s=5, max_bytes=1024
    )
    assert isinstance(result, HttpResponse)
    assert_roundtrip(result)
    await assert_task_cancellation(lambda: http.request("GET", "https://example.invalid/"))


async def assert_proactive_contract(proactive, parts):
    assert isinstance(
        await proactive.submit("test_group", topic="fictional topic", reason="contract"), str
    )
    await assert_task_cancellation(
        lambda: proactive.submit("test_group", topic="fictional topic", reason="contract")
    )
    for kwargs in ({}, {"topic": "topic", "parts": []}, {"parts": parts}):
        try:
            await proactive.submit("test_group", reason="contract", **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid proactive submission accepted")


async def assert_subprocess_contract(process):
    result = await process.run(["fictional-command"], timeout_s=5, env={}, max_output_bytes=2)
    assert isinstance(result, ProcessResult)
    assert len(result.stdout) + len(result.stderr) <= 2
    assert_roundtrip(result)
    await assert_task_cancellation(lambda: process.run(["fictional-command"], timeout_s=5))


def assert_identity_contract(identity, account, actor, platform_id, conversation):
    assert identity.resolve_actor(account) == actor
    assert identity.account_for(actor.actor_id) == account
    assert identity.resolve_conversation(conversation.kind, platform_id) == conversation
    assert identity.platform_id_for(conversation.conversation_id) == platform_id
    assert identity.account_for("fictional-missing") is None
    assert identity.platform_id_for("fictional-missing") is None
