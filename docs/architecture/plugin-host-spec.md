# 插件宿主内部技术约定（A2）

本文件记录按 AGENTS.md 第 4 节向 Claude 只读询问后采用的技术编码。依赖停用、备份、记忆权限和通知需求以用户已确认的现有规格为准；本文件不增加 SDK 扩展点。

## 1. 清单与装配

发现仅读取仓库和实例的插件清单，校验 Schema、SDK 范围、配置 Schema、目录 id、同名冲突、扩展点名称和安全默认值，不执行入口。版本范围由 packaging 处理。

无参数构造 → start(ctx) → factory/handler 装配，装配全部成功后才公开注册项；失败不保留部分注册。start/stop 使用注入时钟的 10 秒超时。行为与上下文段落默认 2 秒，上限 10 秒。其他操作保留 SDK 协作取消语义，A3 负责任务截止和取消宽限期。入站事件保持异步流；导入器完整读取成功后才返回待写入的记录。

api_route、webui_panel 在 A2 只登记并校验命名空间；CLI 处理函数可用假 ApiClient 离线调用，不增加管理 CLI 业务逻辑。定时扩展登记 every/at/days 和处理函数，宿主接收调用方构造的 Tick；不创建真实定时循环或系统任务。

## 2. 计划和启停

内部 API 为 plan_enable、plan_disable、plan_uninstall 与 apply(plan_id)。计划包含操作、伴侣、目标、步骤、错误、依赖图指纹、上次连带停用列表。规范 JSON 的 SHA-256 生成确定的指纹和计划标识；依赖、冲突、版本、启用/失败状态或配置变化后，执行旧计划会报 PlanStale。

启用按依赖拓扑顺序；可选依赖未启用时不自动启用。连带停用按依赖倒序执行，记录保存在 `companions/<id>/plugin-data/_host/cascades.json`，每项包含目标、连带插件、时间和操作标识；计划只读，apply 才写入。返回配置补丁供 A4 API 保存，宿主不改写 companion.yaml。

安装支持已发现的官方包和显式指定的本地包，不下载插件。卸载从本实例的可装配集合移除，标记保存在 `plugins/.host/uninstalled.json`；共享的公开源包不删除，私有状态及版本记录保留。其他伴侣仍运行该插件时，要求先确认它们的停用计划，避免一次确认影响未列出的伴侣。本体模块始终拒绝停用和卸载。

## 3. ctx 与服务

始终属性按 SDK 提供；可选方法只在清单声明、授权且本体注入相应后端时出现。缺少后端就报告启动失败，不创建真实网络、模型或进程后端。memory 的写入、读取、修订分别使用不同的方法集合；调用场景由 ContextVar 绑定，服务/事件/定时/生命周期按 public_group，调用结束后撤销其场景租约。

依赖插件清单中的服务并集为可见服务集合。get 对未声明或停用服务返回 None；已取得的代理每次调用重新检查提供方，失效或处理失败抛 SDK 已有的 ServiceUnavailable。服务失效复用 core 来源的 plugin.disabled 事件，依赖方可通过已有订阅接收，不新增事件类型。

## 4. 私有状态与升级

键值状态保存在插件目录 `.values.json`，键与值大小遵循 SDK 限制；JSON 往返拷贝，写文件后原子替换。宿主版本记录保存在 `companions/<id>/plugin-data/_host/<plugin-id>.json`，插件的 storage 不包含该目录。

迁移脚本命名为 migrations/to_N.py，入口 async migrate(ctx, cancel)。首次安装记录当前 data.version，不跑迁移；已有非空状态缺少版本记录时报错，拒绝隐式迁移或降级。升级前检查连续迁移链。迁移上下文只提供 PluginContext 的始终属性，用于变更本插件状态；不授予快照之外的数据写入能力。

升级接收独立暂存的新版本包。暂停新调用，等待在途调用退出，生成并校验加密快照，再执行迁移和替换包。失败恢复旧状态、旧包和旧版本记录；取消同样先恢复再传播。包交换的旧源保留在插件根内宿主管理的暂存目录，不自动批量删除。加密格式与密钥见 ADR 0004 和 data-and-backup 第 4 节。

## 5. 热加载与索引

清单新增可选 hot_reload 布尔字段，默认 false；true 使用 stop/start 重建并保留私有状态，配置校验失败不动现有实例，不支持时返回 restart_required。新 start 失败按既定启动失败处理；重建期间不分发到已停用的实例，沿用未装配/安全默认值路径。

catalog.json 仅从仓库官方清单生成，不读取实例清单；按 id 和 JSON 键排序，UTF-8、缩进 2、不含生成时间。每项包含清单的 id/name/version/summary/category/dependencies/conflicts/permissions，release_notes 为 null，source 为 `{kind: official, url: null}`，signature 为 null。来源和签名仅预留格式。

## 6. 模型和多账号平台的内部装配单元

宿主 configure_report 只接收 load_home 返回的有效冻结报告；模型与每个平台账号分别使用报告中已校验的第四层最终配置。每组仍按伴侣×插件整体启停、解析依赖和升级，不增加 SDK 字段。

模型单元排第一，随后是 channels 下标顺序；每个单元分别构造并 start(ctx)，全部成功后才装配。各单元只装配自己的 model/channel；只有首个 primary 单元装配其他伴侣级贡献，避免重复行为、服务和定时任务。伴侣级贡献使用 primary 的最终配置，因此调整 channels 顺序会改变其看到的配置。

组内共享 storage 的键空间、events 和 state，state 以最后写入为准。channel_id 使用逻辑 account_ref，身份映射按插件 id 从实例配置构造。任一单元启动或装配失败则倒序 stop，整组不公开注册；停用和热加载也作用于整组。SDK 未提供真实平台登录账号/凭据访问接口，A2 假平台不需要；真实平台接入前必须另行设计，不把这些信息塞进 ctx.config。
