"""记忆启动错误的结构化分类；不从消息文本推断原因。"""


class MemoryStartupError(ValueError):
    code = "migration_blocked"
