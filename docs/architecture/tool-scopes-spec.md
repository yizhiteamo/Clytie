# 工具使用范围规格（B4b）

状态：草案，待用户确认（2026-11-17）。

会话准入（admission-spec）决定伴侣在一个会话里是否说话；工具使用范围决定**谁在什么会话里能用哪个工具**。两者分开配置，都属于本体，不能做成插件。

## 1. 已确认的需求（用户，2026-11-15）

- 画图：群里默认所有人可用；可以排除个别人。
- 画图：私聊只有 owner 能用。
- 陌生人私聊和未登记的群已由准入静默，这里不再处理。

## 2. 配置

伴侣配置 `tool_scopes`（可选，默认为空）：

```yaml
tool_scopes:
  draw_image:
    private: [owner]
    group: [owner, peer, member, stranger]
    exclude: [some_person]
  edit_image:
    private: [owner]
    group: [owner, peer, member, stranger]
    exclude: [some_person]
```

- 键是工具名。没有写进 `tool_scopes` 的工具，不增加额外限制。
- `private` / `group`：在这类会话中可以使用的角色（`owner`、`peer`、`member`、`stranger`）。省略某一项表示这类会话不增加限制；写成空列表表示这类会话中谁都不能用。
- `exclude`：不能使用的人，填实例配置 `people` 中的逻辑 ID。要排除某人，先把他登记到 `people`（角色 `member`）。`owner` 不能被排除。
- 群里没有登记的人角色是 `stranger`，所以"群里所有人"要写上 `stranger`。

## 3. 判定

工具在以下条件**全部满足**时可用：

1. 原有条件：说话人的角色在 `permissions` 中列出了这个工具，并且在工具清单的 `audience` 中（agent-spec §5）。
2. `tool_scopes` 中有这个工具时：会话类型对应的角色列表包含说话人的角色；会话类型不是 `private` / `group` 时不可用。
3. 说话人不在 `exclude` 中。

- 不可用时，原因代码为 `permission`，与原有的权限不足相同。模型看到的说明文字不区分"被排除"和"没有权限"，避免伴侣说出"你被拉黑了"。
- 同一个判定在两个时间点执行：装配本轮工具时（不可用的工具不放进 `ModelRequest.tools`），以及工具真正执行前。执行前再判一次，是为了防止模型调用本轮没有提供的工具；判定失败时工具结果为 `denied`，不调用插件。
- `tool_scopes` 和 `permissions` 一样在伴侣启动时读取，修改后重启生效。

## 4. 配置检查

以下情况配置无效：

- 角色不是四个之一；
- `exclude` 中的 ID 不在 `people` 中，或者是 `owner`；
- `tool_scopes` 的值不是对象，或包含 `private`、`group`、`exclude` 以外的键。

工具名不属于任何已启用插件时，只给出提示，不判为无效（插件可能暂时停用）。

## 5. 不做

- 按单个群设置不同的范围（以后需要时再加 `conversations` 键）。
- 次数或频率限制（boundary.md：不设使用次数上限）。
- 在 WebUI 中编辑（阶段三）。

## 6. 验收

1. 判定表：私聊 owner 可用、私聊 member 不可用；群里四种角色都可用；`exclude` 中的人在群里不可用；`owner` 不受 `exclude` 影响；未写进 `tool_scopes` 的工具行为不变。
2. 不可用的工具不出现在 `ModelRequest.tools` 中，能力状态的原因是 `permission`，说明文字与权限不足相同。
3. 执行前判定：假模型对被排除的人调用了画图工具（本轮没有提供），工具结果为 `denied`，不调用插件。
4. 第 4 节的每种无效配置各有测试。
5. 离线端到端：同一个群里，普通成员能画图，被排除的人请求画图时模型拿不到画图工具；私聊中 member 拿不到画图工具。
6. 双平台 CI 通过；真实平台连接和模型调用次数为 0。
