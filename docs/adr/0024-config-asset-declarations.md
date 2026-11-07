# ADR 0024：必要资产的配置 Schema 遍历

状态：A4.3 实现记录，依据用户批准的 Issue #70 方案 1；尚未完成独立验收。

## 决定

在配置模块提供离线解析器 `resolved_dependencies(schema, finalvalue)`，与清单及
普通配置验证共用。输入是 JSON Schema 与已经合并的最终配置；输出保留每个 JSON
Pointer 和 asset_id。内容去重在捕获层绑定可信伴侣后进行。解析器不打开资产库，
不接收客户端归属声明，不扫描普通文本，不处理插件私有 handle。

清单读取通过 `_Reader.plugin_schema` 校验注解类型、节点位置与可解释性；
`_Reader.validate` 在普通 Schema 验证通过后检查值及有效分支之间的凭据冲突。
`companion_dependencies` 复用 `HomeReport.plugin_configs`，角色插件只使用最终
model、recollect_model、channels 配置，避免把被第四层覆盖的插件基础值再次收集。
该 helper 不以 report.valid 代替调用者对整体报告的判断；涉及资产的配置自身仍须有效。

## 支持范围与失败语义

- 直接字符串标注、properties、patternProperties、additionalProperties、items、
  prefixItems、contains、dependentSchemas，以及嵌套组合。
- allOf 遍历全部分支；oneOf 遍历唯一有效分支；anyOf 遍历所有有效分支。
  if 只用于判定，遍历对应 then/else；不把条件谓词自身当作资产声明。
- 本地 JSON Pointer 和本地 anchor 引用保留原 Schema 根上下文；禁止外部引用。
  带资产注解的 Schema 不支持嵌套资源 $id 或动态引用，明确拒绝，不访问网络或文件。
- not、propertyNames、unevaluatedProperties、unevaluatedItems 以及未知结构内的
  资产声明（包括间接引用）拒绝；不能可靠解释的凭据排除同样拒绝，避免遗漏冲突。
  这些限制仅约束带资产注解的配置 Schema，不缩减既有未声明资产的 Schema 能力。
- 凭据/敏感节点覆盖自身及后代；有效组合分支收集完毕后统一核对覆盖关系。
  可选属性不存在不形成依赖，存在的 null、空串、路径或 URL 都不是 asset_id。
- 实例 notifier 缺少可信伴侣归属时，带资产声明的配置明确失败，不推断共享权限。

普通配置失败保持文件及字段来源定位。错误不包含资产内容或凭据值。恢复流程不能
凭“排除了凭据”猜测组合分支；仍须有足够的认证证据确定生效声明。

## 验证

`tests/test_config_assets.py` 覆盖清单入口、生产 load_home 最终覆盖与错误来源、
组合分支、条件及本地引用、数组嵌套、凭据冲突、非法注解及编号、普通文本不扫描。
测试只用手写假数据和临时目录。
