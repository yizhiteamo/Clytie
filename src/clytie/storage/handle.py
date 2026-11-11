"""事件循环持有的内部存储句柄；实际资源只能在所属线程解析。"""

import threading
from contextlib import ExitStack, nullcontext

from clytie.plugin_host.memory import (
    capture_scene_binding,
    current_conversation,
    current_scene,
    restore_scene_binding,
)
from clytie.sdk import CancelToken
from clytie.sdk.immutable import FrozenDict

from .finalization import current_finalizer, execute_storage_command
from .owner import StorageContext, _snapshot, bind_storage_context, current_storage_cancel


def _argument(value):
    # 取消令牌是受控的执行状态引用，不是数据库对象或可返回的数据快照。
    if isinstance(value, CancelToken):
        return value
    if isinstance(value, FrozenDict):
        return _snapshot(value)
    if isinstance(value, dict):
        return {_snapshot(key): _argument(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_argument(item) for item in value)
    return _snapshot(value)


class StoreHandle:
    def __init__(self, owner, name, store_type, metadata):
        self._owner = owner
        self._name = name
        self._type = store_type
        self._metadata = {
            key: value if key in {"clock", "coordination"} else _snapshot(value)
            for key, value in metadata.items()
        }
        self.changed = None

    def _resolve(self):
        if threading.get_ident() != self._owner._thread.ident:
            raise RuntimeError("storage_requires_async_command")
        return getattr(self._owner._resources, self._name)

    def __getattr__(self, name):
        if threading.get_ident() == self._owner._thread.ident:
            return getattr(self._resolve(), name)
        if name in self._metadata:
            value = self._metadata[name]
            return value if name in {"clock", "coordination"} else _snapshot(value)
        if name == "coordination":
            return None
        if name == "db" or isinstance(getattr(self._type, name, None), property):
            raise RuntimeError("storage_requires_async_command")
        if not callable(getattr(self._type, name, None)):
            raise AttributeError(name)

        def owned_method(*args, **kwargs):
            return getattr(self._resolve(), name)(*args, **kwargs)

        return owned_method

    def for_permissions(self, permissions):
        # 权限视图是无连接包装；插件仍只能取得 SDK 裁剪后的方法。
        from clytie.memory.store import PluginMemory

        if not hasattr(self._type, "for_permissions"):
            raise AttributeError("for_permissions")
        return PluginMemory(self, frozenset(permissions))

    async def invoke(self, function, *args, _permissions=None, **kwargs):
        from clytie.api.management_journal import current as management_context
        from clytie.memory.presence import capture_presence_binding, restore_presence_binding

        scene = kwargs.get("scene", current_scene())
        conversation = kwargs.get("conversation_id", current_conversation())
        scene_binding = capture_scene_binding()
        presence_binding = capture_presence_binding()
        journal = management_context.get()
        if journal is not None and journal.value["companion_id"] != self._owner.companion_id:
            raise ValueError("storage_management_context_mismatch")
        context = StorageContext(
            self._owner.companion_id,
            scene,
            conversation,
            tuple(
                sorted(
                    _permissions if _permissions is not None else kwargs.get("permissions") or ()
                )
            ),
        )
        fixed_args, fixed_kwargs = _argument(args), _argument(kwargs)

        def execute(resources, command):
            store = getattr(resources, self._name)
            coordinator = getattr(store, "coordination", None)
            if hasattr(store, "changed"):
                callback = self.changed
                store.changed = (
                    (lambda: self._owner.loop.call_soon_threadsafe(callback)) if callback else None
                )
            with ExitStack() as stack:
                journal_binding = management_context.set(journal)
                stack.callback(management_context.reset, journal_binding)
                stack.enter_context(restore_scene_binding(scene_binding))
                stack.enter_context(restore_presence_binding(presence_binding))
                actual = StorageContext(
                    context.companion_id,
                    fixed_kwargs.get("scene", current_scene()),
                    fixed_kwargs.get("conversation_id", current_conversation()),
                    context.permissions,
                    context.expected_revisions,
                )
                stack.enter_context(bind_storage_context(actual))
                stack.enter_context(coordinator.commit() if coordinator else nullcontext())
                return function(*fixed_args, **fixed_kwargs)

        return await execute_storage_command(
            self._owner,
            execute,
            context=context,
            finalizer=current_finalizer(self._owner),
            cancel_token=current_storage_cancel(),
        )
