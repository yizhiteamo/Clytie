"""本体事件名称与已规定的负载类型。"""

from .types import (
    BusEvent,
    CompanionStateChangedPayload,
    MessageReceivedPayload,
    PluginChangedPayload,
    QuotaChangedPayload,
    ReplySentPayload,
    TaskStartedPayload,
    TaskSummary,
)

MESSAGE_RECEIVED = "message.received"

REPLY_SENT = "reply.sent"

TASK_STARTED = "task.started"

TASK_COMPLETED = "task.completed"

TASK_FAILED = "task.failed"

COMPANION_STATE_CHANGED = "companion.state_changed"

QUOTA_CHANGED = "quota.changed"

PLUGIN_ENABLED = "plugin.enabled"

PLUGIN_DISABLED = "plugin.disabled"

CORE_EVENT_NAMES = frozenset(
    {
        MESSAGE_RECEIVED,
        REPLY_SENT,
        TASK_STARTED,
        TASK_COMPLETED,
        TASK_FAILED,
        COMPANION_STATE_CHANGED,
        QUOTA_CHANGED,
        PLUGIN_ENABLED,
        PLUGIN_DISABLED,
    }
)

__all__ = [
    "BusEvent",
    "CORE_EVENT_NAMES",
    "MESSAGE_RECEIVED",
    "REPLY_SENT",
    "TASK_STARTED",
    "TASK_COMPLETED",
    "TASK_FAILED",
    "COMPANION_STATE_CHANGED",
    "QUOTA_CHANGED",
    "PLUGIN_ENABLED",
    "PLUGIN_DISABLED",
    "MessageReceivedPayload",
    "ReplySentPayload",
    "TaskStartedPayload",
    "TaskSummary",
    "CompanionStateChangedPayload",
    "QuotaChangedPayload",
    "PluginChangedPayload",
]
