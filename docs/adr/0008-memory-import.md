# ADR 0008：记忆导入计划、分叉历史与导入器候选

状态：实现选型；需求以 memory-spec §9 及 Issue #30 为准。

计划保存在实例 run/memory-plans/<随机标识>.json。只保存目标编号、模式、来源路径定位信息、来源指纹、库指纹、动作、记录编号、可见等级、冲突选择、可信程度降低数量和确认选项，不保存原文或任何解密材料。CLI plan 默认只展示元数据，--show-content 才临时解密显示内容；apply 重新输入口令或恢复码。计划没有自动到期：任何库、包、配置变化都使计划失效。只读计划不修改记忆库；计划文件是管理操作的元数据。

尚无数据库文件时，用内存空库计算只读计划，不生成身份或库文件；apply 持运行锁确认计划后才建立空库，仍先快照并校验，再写入事务。既有库计划用只读连接。候选的规范化内容指纹也存入计划，用于拒绝源字节未变但导入器输出变化的情况。CLI/API 入口提供 SystemClock，测试注入 MockClock；系统时钟的精确适配边界与无库计划实现经 Claude 只读技术确认。

CLI 使用 --choice <record_id>=current|package 逐条指定冲突选择；--confirm-identity 表示用户确认“这是她的记忆”。缺少必需确认时可预览但不可 apply。apply <plan_id> 表示对该完整计划的执行确认。undo <import_id> 生成反向计划，仍经 apply 执行；后续修改冲突使用相同选择编码，package 表示导入前版本。仅包中的声明不能授权越过 UID 检查。

库指纹覆盖规范化的全部逻辑表。源文件用 SHA-256；源目录用排序的相对 POSIX 路径、大小、内容哈希计算总指纹，不包含绝对路径、不跟随符号链接。来源定位路径只保存在本机计划，不进入修订与导出包。确认时取得运行锁，重新计算指纹，再快照校验、单事务提交。undo 也先快照；快照清理逐个删除明确文件并审计。

原生 new 和未分叉 merge 保留来源修订。分叉时额外保存来源历史；使用包版本或 restore 时追加本地修订，不覆盖旧记录。来源会话在当前配置缺失时，保留原历史并追加 origin=null 的规范化修订，持久记录缺失来源数量。导入前后版本通过 imports 关联表保存；undo 只反转这个关联表中的变化，恢复也是新修订。每次成功导入有独立编号，undo 本身亦有编号。

导入器返回 ImportCandidate，不返回 MemoryRecord。候选字段为稳定 record_id、kind、content、visibility、created_at、updated_at、retracted、tags、source_ref，以及 subject_account={channel,account}、origin_conversation={channel,kind,platform_id}、proposed_basis（默认 observed）。channel 为插件 ID，与实例配置精确匹配，不是伴侣账号引用。缺少或无法唯一映射任一引用时，subject/origin 同时为 null，basis 为 observed。仅明确同意提议且映射到 owner 时采用 owner_*；source_type 由本体填写，未知固定 group，仅作说明。候选重复 ID 或类型错误令整个计划失败。候选没有本地 revision，修订号由本体分配。

导入器再次导入仍以 new 请求，本体动作标为 reimport；仅 UID 来源插件、源指纹一致，且所有修订来自该来源导入或相应 undo 才允许。有效记录相同则跳过，撤销后重做追加修订；可信程度以本次选择为准，降低数量单列，整体确认（Issue #30）。空结果拒绝，避免为没有记录的试导入占用身份。

本体关键词/语义固定两路不增加第三路。A3.3 不调用既有 retriever 扩展；CandidateReader 的投影与返回 ID 交集仅提供适配器及独立测试，不宣称已有生产调用路径。以上导入候选编码与最小检索范围经 Claude 只读技术询问确认，未改变身份、场景、权限或可信程度需求。


第 2 轮验收后，CLI `memory import plan` 用 `--importer <插件ID>` 选择已启用的清单导入器，用 `--approve-proposals` 明确同意可信提议；不提供该选项即不同意，计划仍列降低数量，执行时整体确认。MemoryAPI 只启动所选导入器及已启用的必需依赖，不加载其他伴侣插件，结束时关闭。候选经宿主 invoke 消费 SDK 异步迭代器，每项校验 ImportCandidate；收集使用注入时钟的 30 秒上限和取消检查，故障由宿主记录。导入器输入为用户提供的源副本，不走包口令交互。此处为已有导入器框架的技术接入，不改变来源映射和用户确认规则。


Issue #32 F2 技术澄清（Claude 只读确认）：修订 hash/parent 仍包含 origin_conversation_id，不能从持久指纹中删除隐私来源字段。共同祖先比较临时按当前配置规范化来源，折叠相邻等价指纹，避免仅追加 origin=null 修订产生假分叉。只有原始链为前缀时才直接保留来源修订；仅规范化链等价时追加本地修订并另存包历史。计划仍绑定配置；日后补回来源会话时保守地允许出现需用户选择的真实比较冲突，不自动扩大可见范围。


Issue #32 F5 技术边界：SDK 的 detect 仍是同步 bool 协议（Claude 只读核对），装配拒绝异步函数，调用拒绝并关闭协程返回值，不以无限 await 扩大协议。管理适配器等待宿主 invoke 的路径使用注入时钟 30 秒上限；MockClock 回归覆盖超时和取消。同步插件代码阻塞事件循环无法由同线程协作超时抢占，此限制不以后台线程假装可取消；进程级隔离不在本次修复范围。
