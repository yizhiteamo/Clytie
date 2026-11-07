# 插件宿主内部技术约定（A2）

本文件记录按 AGENTS.md 第 4 节向 Claude 只读询问后采用的技术编码。依赖停用、备份、记忆权限和通知需求以用户已确认的现有规格为准；本文件不增加 SDK 扩展点。

## 1. 清单与装配

发现仅读取仓库和实例的插件清单，校验 Schema、SDK 范围、配置 Schema、目录 id、同名冲突、扩展点名称和安全默认值，不执行入口。版本范围由 packaging 处理。

无参数构造 → start(ctx) → factory/handler 装配，装配全部成功后才公开注册项；失败不保留部分注册。start/stop 使用注入时钟的 10 秒超时。行为与上下文段落默认 2 秒，上限 10 秒。其他操作保留 SDK 协作取消语义，A3 负责任务截止和取消宽限期。入站事件保持异步流；导入器完整读取成功后才返回待写入的记录。

api_route、webui_panel 在 A2 只登记并校验命名空间；CLI 处理函数可用假 ApiClient 离线调用，不增加管理 CLI 业务逻辑。定时扩展登记 every/at/days 和处理函数，宿主接收调用方构造的 Tick；不创建真实定时循环或系统任务。

## 2. 计划和启停

内部 API 为 plan_enable、plan_disable、plan_uninstall 与 apply(plan_id)。计划包含操作、伴侣、目标、步骤、错误、依赖图指纹、上次连带停用列表。规范 JSON 的 SHA-256 生成确定的指纹和计划标识；依赖、冲突、版本、启用/失败状态或配置变化后，执行旧计划会报 PlanStale。

启用按依赖拓扑顺序；可选依赖未启用时不自动启用。连带停用按依赖倒序执行，记录保存在 `companions/<id>/plugin-data/_host/cascades.json`，每项包含目标、连带插件、时间和操作标识；计划只读，apply 才写入。返回配置补丁供 A4 API 保存，宿主不改写 companion.yaml。A4.2 由 API 将 apply 返回的 patch 写回配置；注释/顺序/格式保留、写前副本、逐个历史文件清理、写后校验还原及 applied_not_saved 提醒按 `api-spec.md` §4.4。

代码由外部更新；宿主不安装、移动、替换或删除插件代码。卸载仅从本实例的可装配集合移除，标记保存在 `plugins/.host/uninstalled.json`；公开源包、私有状态及版本记录保留。其他伴侣仍运行该插件时，要求先确认它们的停用计划，避免一次确认影响未列出的伴侣。本体模块始终拒绝停用和卸载。

## 3. ctx 与服务

始终属性按 SDK 提供；可选方法只在清单声明、授权且本体注入相应后端时出现。缺少后端就报告启动失败，不创建真实网络、模型或进程后端。memory 的写入、读取、修订分别使用不同的方法集合；调用场景由 ContextVar 绑定，服务/事件/定时/生命周期按 public_group，调用结束后撤销其场景租约。

依赖插件清单中的服务并集为可见服务集合。get 对未声明或停用服务返回 None；已取得的代理每次调用重新检查提供方，失效或处理失败抛 SDK 已有的 ServiceUnavailable。服务失效复用 core 来源的 plugin.disabled 事件，依赖方可通过已有订阅接收，不新增事件类型。

## 4. 私有状态与升级

键值状态保存在插件目录 `.values.json`，键与值大小遵循 SDK 限制；JSON 往返拷贝，写文件后原子替换。宿主版本记录保存在 `companions/<id>/plugin-versions.json`，只有宿主能读写，不在插件 storage 或升级快照内。

迁移脚本命名为 migrations/to_N.py，入口 async migrate(ctx, cancel)。首次启用按 plugins.md 第 6.1 节正常启动并记录当前版本，不跑迁移。升级前检查连续迁移链。迁移上下文只提供 PluginContext 的始终属性，用于变更本插件状态；不授予快照之外的数据写入能力。

就地升级以 plugins.md 第 6.1 节为准，不提供独立新包切换入口。仅插件版本变化时兼容性检查后正常启动、更新记录并发出已更新事件；数据版本增加时保持待迁移，生成只读计划，经确认且代码与私有状态均未变化后，确认插件未运行，再快照校验、逐步迁移。全部脚本成功后，只更新本插件的版本项、删除迁移日志，再启动插件。降级或缺少迁移步骤拒绝启动。提交前的迁移失败只恢复私有状态、保留旧版本记录，插件保持迁移失败且不启动；提交前取消同样先恢复再传播，不改写 enabled。提交后的启动失败或取消按普通启动失败处理，保留新数据和新版本。

恢复及故障目录的提醒、确认清理遵循 data-and-backup 第 4.2 节：先解密到实例内临时目录并校验，将故障状态目录整体改名保留，再将临时目录改名为状态目录。失败立即停止并报告位置；未经确认不清理故障目录。迁移日志、"恢复未完成"状态、私有状态目录缺失时的处理及解除方式见 data-and-backup 第 4.3 节。加密格式与密钥见 ADR 0004 和 data-and-backup 第 4 节。

## 5. 热加载与索引

清单新增可选 hot_reload 布尔字段，默认 false；true 使用 stop/start 重建并保留私有状态，配置校验失败不动现有实例，不支持时返回 restart_required。新 start 失败按既定启动失败处理；重建期间不分发到已停用的实例，沿用未装配/安全默认值路径。

catalog.json 仅从仓库官方清单生成，不读取实例清单；按 id 和 JSON 键排序，UTF-8、缩进 2、不含生成时间。每项包含清单的 id/name/version/summary/category/dependencies/conflicts/permissions，release_notes 为 null，source 为 `{kind: official, url: null}`，signature 为 null。来源和签名仅预留格式。

## 6. 模型和多账号平台的内部装配单元

宿主 configure_report 只接收 load_home 返回的有效冻结报告；模型与每个平台账号分别使用报告中已校验的第四层最终配置。每组仍按伴侣×插件整体启停、解析依赖和升级，不增加 SDK 字段。

模型单元排第一，随后是 channels 下标顺序；每个单元分别构造并 start(ctx)，全部成功后才装配。各单元只装配自己的 model/channel；只有首个 primary 单元装配其他伴侣级贡献，避免重复行为、服务和定时任务。伴侣级贡献使用 primary 的最终配置，因此调整 channels 顺序会改变其看到的配置。

组内共享 storage 的键空间、events 和 state，state 以最后写入为准。channel_id 使用逻辑 account_ref，身份映射按插件 id 从实例配置构造。任一单元启动或装配失败则倒序 stop，整组不公开注册；停用和热加载也作用于整组。SDK 未提供真实平台登录账号/凭据访问接口，A2 假平台不需要；真实平台接入前必须另行设计，不把这些信息塞进 ctx.config。


## 7. 操作锁与解除意图的内部编码

按用户确认的 data-and-backup 第 4.3 节实现。锁文件为 `companions/<cid>/plugin-recovery/<pid>.lock`，稳定保留，不通过删除文件抢锁。Windows 使用非阻塞 msvcrt 字节锁，Linux 使用 flock；进程内持有表防止同进程重复进入，多插件按 id 排序一次取得全部锁。锁文件记录 pid、OS 进程创建时间、操作名称和释放状态；OS 锁是持有者存活的判据。版本记录读取不取排他锁，原子替换保证文件完整，Windows 版本读取句柄允许删除共享，写锁内对短暂共享冲突有界重试原子替换；仅读改写通过 `plugin-versions.lock` 保护，使用有上限的轮询等待（最多 500 次等待，每次 0.01 秒），不使用 `time.sleep`。等待超时明确抛出版本写锁超时异常，不能伪装成插件操作冲突、启动失败或恢复未完成；普通启动清理后保持停用。迁移提交点写入失败返回迁移失败，并保持迁移日志及旧版本记录，提示下次加载按第 4.3 节自动恢复。插件操作锁仍按已确认规则立即报告冲突。

解除日志的 resolution 保存计划标识、方式、版本选择、完整目标版本项与执行进度。写版本前持久化 writing，写后标记 written；在 writing 崩溃时比较当前项和确定目标值识别提交。已提交标记跨重试保留；未提交的受控失败可重新规划。选择、失败与旧选择分别追加审计。损坏日志的接受计划仅依据规范私有目录与用户输入，不使用损坏日志中的路径；执行前生成合法恢复日志，仍按先写版本再删日志完成。

这些编码来自只读技术询问和用户后续确认；不改变数据版本、场景、取消或 SDK 的既定规则。


第 6 轮用户确认的补充：close 的集合构造、停止排序及执行按伴侣隔离异常，结果记录每个伴侣 stopped / not_stopped 与原因；停止只排序运行集合中的依赖边，缺失依赖不阻止关闭，依赖方先停，无法排序的剩余项按 id 停止。多插件启用遇到 MetadataLockTimeout 时，异常的 enabled（元组）与 patch（配置补丁）包含本次已完成启用的插件，不包含此前已运行或本次失败的插件。

关闭被取消和同一伴侣内继续停止其余插件的后续规则，见 `../design/supervisor.md` 的“安全停止（A4，用户已确认）”；在 A4 验收实现。

## A4.1 实例范围装配与关闭

按 `supervisor-spec.md` §5.1，新增只用于 notifier 的实例装配范围，沿用宿主装配路径和清单校验规则。列表取全局 `notify.plugins`，ctx 只有 config、log、clock、subprocess，没有伴侣、记忆、账本、平台或模型。伴侣级通知仍使用自己的通知插件。

按 §6，close 按依赖方先停逐个停止，单个插件异常后继续停止其余插件，把失败插件与原因记入 not_stopped；一个伴侣失败不影响其他伴侣。关闭取消在所有伴侣完成后传播，完整结果由 ShutdownCancelled.result 携带。

## A4.3 捕获与插件生命周期（待实现）

以下为已批准的 A4.3 契约，尚未实现、未经验收；完整依据为 `backup-spec.md`，不代表历史里程碑已提供这些能力。

协调器准备 → 取得可证明的组件快照边界 → 释放业务协调 → 后台复制/压缩/加密/校验。SQLite 使用独立连接/事务和在线快照能力，不直接复制活动 db/wal 文件，不在 API/聊天事件循环执行大文件同步处理。
单数据库一致不足以证明完整实例一致。实现 ADR 必须列出逻辑一致性组：至少 memory 与其回想进度/所引用账本、插件状态与其版本/清单/配置、管理提交证据与对应数据、资产引用与内容。为组建立共同捕获边界或可核验版本链；不能拿各自不同时刻的有效文件冒充同一组快照。
跨组可以记录不同捕获时间，不承诺整个世界在同一微秒冻结；组间有引用的也必须核验。外部消息送达事实不可能靠本地快照倒退，恢复按 §9。
本体采用跨进程协调捕获与短暂提交门闩，固定锁顺序；捕获前等待组内正在提交的逻辑操作到安全边界，固定数据库读视图/文件版本后立即释放。长时间拷贝、加密、校验不得持有业务提交门闩。每次捕获门闩持有工程默认上限 250 毫秒、等待上限 2 秒；超时放弃本次捕获并释放，不能为凑成功延长聊天阻塞。已持有的 SQLite 视图/不可变文件持续有效至物化完成，之后释放；不得先释放再读取活动文件。
迁移、restore/apply、版本升级、配置切换等跨组件变更与该捕获边界互斥；不能调用危险操作的恢复入口来取得备份。遇到正在迁移或恢复受阻，标记组件 unavailable，本次为 partial/failed，原运行/恢复规则不变。
备份最多单工作进程、分批 I/O、可取消，忙时让步；不承诺固定耗时。若固定视图导致 WAL 或临时空间快速增长，取消普通备份并释放视图，不能拖垮业务磁盘。阻塞/异常的 provider 只能使备份失败/部分，不得通过杀伴侣或偷偷重启来解锁；违反插件契约的情况单独审计。

这是在线备份要求下的新公共契约，须先同步 sdk-spec、清单 Schema 和 CHANGELOG，再实现。插件仍只 import clytie.sdk。
清单新增可选 `backup`：`mode` 为 `stateless | snapshot | unsupported`；省略按 unsupported。snapshot 模式需在 `contributes` 声明唯一 `backup.snapshot` factory，按现有 factory 约定返回提供者；其他模式不允许声明。stateless 表示没有需恢复的业务持久状态，宿主发现 plugin-data 内非声明缓存数据时拒绝该结论并报缺项。缓存不能由插件临时声明来掩盖已有业务数据。
已停止且无活跃执行者/恢复日志的插件，可在取得原生命周期及数据锁后做离线一致复制；无需为备份启动插件。运行中的 unsupported 插件有状态时为缺项。支持状态在列表、启用计划及 status 展示，旧插件仍能正常启用，不把新增字段变成隐式停用。

公共协议（以下 ticket/handle 是进程内对象，不持久化或跨 IPC 序列化）：

```
async prepare(request: BackupRequest, cancel: CancelToken) -> BackupTicket
async capture(ticket: BackupTicket, epoch: str, cancel: CancelToken) -> BackupView
async materialize(view: BackupView, sink: BackupSink, cancel: CancelToken) -> None
async release(ticket: BackupTicket) -> None
```

- `BackupRequest`：operation_id、companion_id、plugin_id、目标 data_version、准备 deadline；这些由宿主填充。提供者不自行选择其他实例/伴侣。
- prepare 在捕获门闩外准备资源，可在私有临时区域工作，不停止插件、发送消息、调用模型或改变业务事实；默认 10 秒。capture 在组捕获窗口内固定该 epoch 的稳定视图，不能在这里做大文件复制。准备期间业务改变时须重新验证，不允许返回旧快照却标上新 epoch。
- `BackupView`：epoch、data_version、revision（提供者的稳定版本标识）以及提供者私有只读句柄。提供者保证 materialize 对应 capture 版本；宿主验证 epoch/版本、完整成员和关联，无法证明则不能算完整。实现允许不可变版本、数据库读视图等方式，不要求所有插件采用同一种存储。
- `BackupSink` 只允许 `async write(relative_path: str, chunks: AsyncIterator[bytes]) -> BackupEntry`。宿主计算字节数与哈希；返回相对路径、size、sha256。路径限定当前插件备份命名空间，拒绝重复/穿越/链接，不把任意宿主文件句柄交给插件。sink 拒绝写入声明为凭据的内容；内容完整性仍由插件契约和测试保证，不以扫描代替。
- materialize 在门闩释放后运行，只读稳定视图；取消/超时后不再接受其迟到写入。release 在成功、失败、取消时均执行且应幂等，默认 2 秒；记录释放失败，不吞掉它导致的资源泄漏。迟到资源继续被跟踪，不能把失去跟踪的 provider 说成已结束。
- 该接口提供备份快照，不授予通用恢复执行脚本权限。每个 sink 相对路径精确映射到恢复目标 companions/<cid>/plugin-data/<pid>/ 下的同一相对路径；成员必须是同一 data_version 可直接加载的完整持久布局。数据库须输出可直接打开的一致数据库文件，不接受需要执行 SQL 导入脚本、反序列化可执行对象或运行转换程序才能还原的逻辑导出。需要转换的 provider 本阶段视为 unsupported，结果 partial。宿主只做安全文件重建和格式校验，插件只在用户后来正常启动时按原生命周期加载。
