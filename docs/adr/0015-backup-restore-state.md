# ADR 0015：新目标恢复、维护入口与凭据校验

状态：A4.3 实施中的技术约定；产品依据为 backup-spec §8、§12、Issue #64 的方案 1A。尚未实现验收，不改变普通启动规则。

## 决定

恢复管理使用现有本机 Server/Auth，CLI 仍是 API 客户端。maintenance 是显式进程启动模式，不加载业务插件，不启动伴侣或自动备份。管理目录与尚未完成的目标目录隔离。

恢复计划记录 schema_version=1、id、source_instance_id、包 SHA-256、密钥指纹、目标 canonical path、目标目录身份、配置与组件校验结果、credentials_missing、空间估算和源停用要求。计划只持有明确密钥文件的路径及指纹，不持有密钥正文。执行时重验路径、包、密钥、目标身份和空间，不信任客户端给出的风险等级。

目标先持独占 OS 锁，再写恢复记录；状态为 incomplete → verifying → ready_for_switch。逐项成员写入和校验的证据先于 ready 标记。准备失败及取消保留 incomplete，禁止所有启动；恢复仅核验本次明确成员并完成必要提交，不重新运行解密恢复请求。不能证明完整性时为 recovery_blocked，保留证据，不删除源或旧实例。

ready_for_switch 不代表正常业务配置有效。维护 status 分别报告 data_state、credential_exclusions、configuration.valid/global_errors/companions 和 source_stopped。恢复校验仅放行认证清单精确对应、Schema 声明证实的凭据缺项；普通 load_home 不接受任何排除参数，也不从恢复记录取得豁免。诊断使用结构化错误分类与字段路径，不解析展示文本。

source_stopped 与 switched 是独立、幂等的确认操作。维护模式在目标本地核对恢复绑定，登记源停用；同机源活跃时拒绝。普通启动重新完整检查配置及源运行状态，不复用此前 status 的成功结论。switched 仅能在正常启动后由目标 API 核对实际运行结果再登记。任何一步都不修改系统环境变量、不自动启动平台或复制第二套目标目录。

## 安全与失败边界

- 配置有效、文件存在不证明凭据可登录；不调用真实模型/平台验证。
- 所有目标及清理路径逐组件拒绝链接/reparse point、别名、越界和未知文件；不递归删除。
- 原执行者、运行锁和会话凭据不恢复为活动状态；已完成操作证据保持历史含义。
- 源包在计划、执行和核验期间受引用保护；释放依据持久阶段及进程身份，不能仅凭 HTTP 请求结束。
- 恢复错误分开保存已完成数据和启动未就绪，不因响应丢失改报未执行，不重放业务任务。
- API 错误以稳定 code 驱动，未知原始异常只记固定原因；私密内容、密钥和口令不写日志。

## 验证

验收 15a 必须走真实回环 API/子进程：维护查询 → 补齐虚构凭据 → 源停用确认 → 普通启动 → 登记切换；另验证修改配置后不能借旧成功结果启动。恢复文件/发布中断须使用真实进程强杀验证，不能以手工构造状态替代生产路径。

## 恢复核心清单与状态技术约定

认证元数据 schema_version=1、scope=instance、completeness=complete，code_version 必须等于当前安装发行版本，不执行隐式迁移；source_instance_id 为来源实例标识。companions 为 id/uid/memory/ledger 列表，后两者是包内路径。consistency_groups 每项 epoch/members/sha256，覆盖所有实际成员并逐项匹配容器清单；每伴侣 memory 与 ledger 必须处在同组。assets 为 asset_id/path/sha256 列表；plugins 为 companion_id/id/version/data_version/manifest_path/state_paths/version_path 列表。插件代码及数据版本、实际配置启用项不得被清单遗漏。credential_exclusions 仅传给独立凭据证明校验器。

source_registration 含 machine_id（本机标识摘要）、home、identity（device/inode）；属于认证私有清单。生产停用检查验证同机目录身份、supervisor 与各伴侣 ledger/memory OS 锁、锁中 PID 创建身份。异机明确 remote_unverifiable；同机失联/身份不符/锁状态不可核验为 unavailable，拒绝自动判停。所有切换确认仅记录事实，不启停源或目标。

管理 home 的 supervisor/restore-plans/<id>.json 保存计划与认证清单；目标 supervisor/restore.json 保存 target 路径与目录身份、操作编号、包摘要、独立目标实例 UUID、来源和阶段。计划 workspace 必须是调用方提供的独占空临时目录，计划解包只用于校验；不自动删除。apply 直接在目标内重建，原文件无需第二次搬运。目标旁稳定锁文件跨不同管理 home 竞争同一目标；首次独占创建 marker 前再次核对目标指纹。container 的 owned_marker 参数仅容许内部精确文件，绑定内容 SHA256 与 stat 身份；目录其他现存内容一律拒绝，包不得覆盖 marker。

阶段 incomplete → verifying → ready_for_switch；异常保留 incomplete/recovery_blocked，不重放。解包全部结束且清单和源哈希相符才持久 verifying；强杀后仅 verifying 可重新检查已存在明确成员、数据库与关联并提交，incomplete 不重跑 unpack。已提交重复 apply 返回同一结果。source_stopped 与 switched 记录独立确认；普通启动必须重新执行配置校验与来源核验，不能凭旧 configuration.valid 放行。配置凭据缺项独立展示。

恢复细化：首条记忆之前，认证 uid=null 且数据库无 UID、无当前记录和修订时允许保持空身份；绝不在恢复中生成伴侣 UID。plugin 清单声明的必要状态目录可以为空，验证后只重建对应空目录，不创建数据库。目标 marker 中 audit 与确认事实原子提交，source_stopped/switched 各有稳定事件编号，重复调用不重复登记。源同机登记失联或明确访问不可达显示 inaccessible，只有本次确认或已持久确认才接纳该不可核验条件；任何重新发现的活跃锁/相同创建身份仍阻止启动。目标绑定错配或损坏不作为不可达放行。

逐块空间检查在没有外部 check 回调时仍执行；回调只补充取消或更严格预算。临时计划解包与目标恢复均不覆盖既有文件。普通启动配置分层继续由守护进程原 global/companion 校验负责，恢复 guard 不把单伴侣错误升级为整个实例错误。

## 旧实例登记与逐项清理

运行日志由每个长期运行进程自己持有 stdout/stderr 专用管道和非 daemon 排空线程；伴侣及前台守护 stdout 仍保留控制协议，只有后台守护将 stdout 一并接入日志。恢复写端后等待 EOF 与线程结束，写入故障向入口传播；短命启动客户端不持有长期日志句柄。分卷按字节限制、单条写入跨进程锁协调；审计不执行普通日志淘汰。完整包保存封存卷的逻辑路径、长度和摘要索引，恢复后核验正文再建立目标文件身份，不使用源机器 inode。

切换确认在写入 switched 之前按 operation_id 幂等登记旧实例，崩溃重试不重复登记。完整包携带旧实例登记摘要；原 authority 绑定保留，复制到新 home 不因此得到原路径的扫描或删除权。占用观察使用文件属性，不为 status 重新读取全部数据内容；清理计划仍完整验证内容指纹。

登记保留 source/target instance ID、operation ID、canonical 目录与 device/inode、来源 machine 摘要及管理目录自身身份。多轮登记追加，不覆盖历史；跨机及拷贝来的登记不能凭原路径取得删除权。显式本机接纳须重新核对原登记的路径和文件身份，失联显示 unknown，不计为零占用。

清理计划只读旧目录，管理目录保存固定清单。清单逐项含路径、身份、长度、mtime、SHA-256、验证保留位置及唯一密钥/独有数据说明；当前实例同相对路径或用户显式副本，必须同哈希、不同文件身份且不在待清理树内。此证据仅证明字节保留，不证明可启动或已完成恢复演练。找不到有效副本、唯一密钥未另存、来源未接纳或存在恢复引用时阻止清理。

`startup_gate(home)` 是目录旁稳定 OS 门闩。清理全过程持门闩；启动取得 supervisor/业务运行锁期间须持同门闩，防止 inactive 检查后的启动竞态。清理仍逐次检查实际 supervisor/ledger/memory OS 锁及进程创建身份，不用调用方 bool 证明停用。

apply 重新核验整个剩余文件/目录集合、原登记与所有保留副本；每次只 unlink 一个计划文件，先写 deleting 意图、成功后记 deleted 事实。按明确清单由深到浅 rmdir 空目录，最后 rmdir 原根。任何新对象、链接、身份/哈希变化或权限失败使本计划停止，实际已删除条目保留；旧计划不能恢复执行，重新生成计划后再确认。强杀留下 applying/deleting 也不自动重放。所有审计保存在管理目录，不放入正在删除的源目录。

旧实例清理前须检查旧 home 的源仓库权威引用及遗留引用；未知登记失败关闭。apply 在首次删除前用源仓库短事务登记 repository_retirement，原子确认没有引用并禁止新内容读者，不在整个逐文件清理期间长持引用锁。记录本次造成的引用账本和锁文件变化，其他清单变化仍使计划失效。崩溃遗留 retirement 保持拒绝读取；持启动门闩确认清理已停止的恢复路径才可释放同一操作的 retirement，之后必须重新计划。已删除仓库元数据使来源绑定失败，不重建空登记恢复读取权限。

## 第 1 轮审查：组合声明与源实例目标边界

凭据位置复用受控本地 Schema 引用解析，支持无条件 allOf 与嵌套对象、数组声明。无法在剔除后精确重建的条件声明以及不支持的位置失败关闭，标为 opaque_credential_config；false 注解不构成凭据声明。恢复仍逐字段比较认证清单、Schema 摘要和结构化诊断，不把其他配置错误当作缺少凭据。

同机认证清单中的 source_registration.home 只作为禁止目标重叠的边界，不产生读取、搜索或管理历史路径的授权。不对它 resolve/stat；对用户明确目标继续执行既有路径和链接校验。生成、重复查询、接受执行、apply 与 recover 均检查该边界，拒绝源实例本身及其祖先/子目录。管理 home 与源仓库的既有保护同时保留。
