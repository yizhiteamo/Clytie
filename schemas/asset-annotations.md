# 配置 Schema 的必要资产注解

插件清单的 config Schema 可在具体字符串节点声明 `x-clytie-asset: true`：

```json
{
  "type": "object",
  "properties": {
    "portrait": {"type": "string", "x-clytie-asset": true},
    "attachments": {
      "type": "array",
      "items": {"type": "string", "x-clytie-asset": true}
    }
  }
}
```

注解值必须是布尔值；false 不声明依赖。声明值必须是 64 位小写十六进制内容
SHA-256，不是文件路径、URL 或 AssetRef 对象。不要在数组/对象整体标注。
可选字段未提供时没有依赖；提供 null 或空串会校验失败。只解析最终合并生效值。

同一字段或其祖先不能同时受 `x-clytie-secret` / `x-clytie-sensitive` 排除覆盖。
组合与条件使用实际有效分支，anyOf 任一有效分支的声明都不能被其他分支抵消。
支持与限制见 [ADR 0024](../docs/adr/0024-config-asset-declarations.md)。
校验不会读取资产数据库；捕获时才由本体按可信上下文核验登记、权限及内容。
