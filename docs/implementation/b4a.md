# B4a 会话准入

## 结论

本体在 Agent 认领入站之前检查准入，交付层在 send/react 之前再次检查。
配置在伴侣启动时固定；修改后重启生效，没有运行中重载接口或文件监听。
registered 默认只接收登记群及 owner/member 私聊；test_only 限定测试会话和预期发言人，陌生人私聊始终拒绝。
拒绝入站只增加原因计数；拒绝出站生成 failed/not_admitted 永久收据，不调用平台、不重试。
计数经伴侣心跳进入健康状态和本地 API，只保留模式及本期原因的非负整数。

## 验证与限制

准入表、配置、静默副作用、出站收据和状态链路见 tests/test_admission.py。
假协议端经模型、画图工具再返回协议端的双群验证见 tests/test_openai_api_integration.py。
所有验证使用虚构数据及假后端，没有调用真实平台或模型。
计数是本次伴侣运行期的计数，重启后重新计数。
owner_present、成员查询和 tool_scopes 属于 B4b，本期不支持。
Issue #105 的配置生效时点已由 PR #106 明确；实现没有偏离该规格。
