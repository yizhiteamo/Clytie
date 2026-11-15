# B3a：openai-api

实现普通 Chat Completions 模型插件，复用宿主 HTTP、凭据和资产服务。本体工具循环不变；仅在 `_generate` 增加 `model.usage` 审计，不迁移账本。Issue #97 的规格为 agent-spec §11.1。

离线测试覆盖模型契约、工具往返、图片资产授权读取、错误分类、取消、截止和用量；完整假协议端链路经过 qq-onebot、openai-api、image-gen 及真实账本，并检查凭据不进入日志或账本。双平台 CI 和 Claude 审查尚待完成。

已知限制：Chat Completions 不提供可靠余额查询，返回未知；图片需要已授权 AssetRef，不解析或下载平台 raw_ref。上下文字符预算和模型能力需按部署填写，不探测真实模型。没有线上 API 或真实平台验证。
