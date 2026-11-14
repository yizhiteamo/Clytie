# ADR 0025：旧 SQLite 的账本 JSON 索引核验

状态：实现兼容修正，不改变备份格式、恢复语义或运行权限。

## 证据

PR #71 首次 Windows CI 的恢复链路在读取 sqlite_master 时失败，原因为
`unsafe use of json_extract()`；Linux 同候选通过。本机 SQLite 的 JSON
函数带 INNOCUOUS 标记，不能仅以本机通过覆盖其他发行构建。
SQLite 官方已记录 JSON 内建函数标志错误：
https://sqlite.org/forum/forumpost/c88a671ad083d153?t=c 。

用虚构生产账本，在测试连接上注册 deterministic 但不带 INNOCUOUS 的同名
JSON 函数，稳定复现同一失败。测试不下载 SQLite，也不修改系统库。

## 决定

普通连接继续 trusted_schema=OFF。仅恢复账本、且实际 function_list 显示
json_extract 缺少 INNOCUOUS 时，进入兼容路径。连接始终以 mode=ro、immutable
打开并保持 query_only；不修改源文件、库中 Schema 或全局设置。

该路径安装只读 authorizer，拒绝未列出的操作和函数；仅准许所需读取、核验
PRAGMA、json_extract、like 及既有进度边界查询的 coalesce/max。此连接使用 trusted_schema=ON 以解析受影响的
表达式索引，但在任何数据查询和 integrity_check 前，逐项比对整个 Schema：
名称、对象类型及 SQL 必须与本体建库语句生成的 sqlite_master 完全一致。
只允许历史账本缺少两个后加的健康索引。未知/修改对象立即拒绝，不执行包内
SQL，不根据错误消息放行，也不在失败时降级为跳过核验。

账本建库语句提取为共享常量，实际 DDL、模式版本及正常启动行为不变。
记忆库与单独记忆包不进入兼容路径。未受影响的 SQLite 仍使用原策略。

## 验证边界

回归直接核验生产账本、字节不变、只读和函数拒绝；篡改索引、视图、触发器、
CHECK 及额外索引均拒绝，危险回调不执行。另检查缺少可选健康索引兼容，以及
记忆库仍关闭 trusted_schema。故障注入验证函数标记分支，实际受影响构建由
Windows CI 的完整恢复、加密包及真实进程链路验证；两种证据分别保留。
