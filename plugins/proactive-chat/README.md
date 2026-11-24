# 主动私聊

把 `conversation` 设为实例里登记的主人私聊逻辑 ID。插件按实例时区安排早安、久未聊天和节日话题。每次只向本体提交内部说明，伴侣自行决定说什么、是否使用能力。可用能力及每日额度取自伴侣配置的 `proactive.tools`。

```yaml
conversation: test_private
topics: [good_morning, long_silence, festival]
```

- `owner_rest` 设置主人的休息时段。`companion_rest.workday` 和 `companion_rest.restday` 分别设置伴侣在工作日和休息日的时段。每个时段都用 `from`、`to` 表示本地时间。
- `data/calendar-<年份>.json` 提供法定节假日、调休上班日及节日。缺少某年放假安排时按周末划分休息日，该年已知的节日仍可使用。
- `GET /plugins/proactive-chat/status` 显示当日尝试、开口与放弃次数、连续未回次数、最早下次开口时间，以及日历和配置状态。确定送达和结果未知都计作一次开口。分段消息按同一次开口计算。
- 目标会话正忙或主动聊天已暂停时，下一次定时检查会重新判断。每次获准提交的编号会保存在插件状态中，重启后由本体核对。模型决定不说时，同一话题等待 1–2 小时。中断、失败或编号过期时等待 30 分钟，之后可以发起新的一次。

启用新闻订阅后，`proactive.news` 服务接收 `news-feeds` 提供的公开材料。每分钟检查一次，能马上说的新闻合成一次开口。主人休息时，最近 30 分钟在她看得到的私聊或群聊中发过言就算醒着。配置 `breaking_at_night: always` 可以让她半夜直接说。

每日一报在早安后 30–90 分钟安排。没有早安时从伴侣的休息结束算起。她从至多五个候选中挑一两件，也可以都不说。到 14:00 结束当天的简报窗口。状态中的 `breaking_today` 按开口次数计，`brief_sent_today` 表示当日简报已经送达或发送结果未知，`waiting_for_daylight` 是当前因主人休息而等待的新闻件数。

在仓库根目录运行 `python -m pytest tests/test_proactive_timeline.py -q -s`，可以查看虚构的三天时间线，包括每次尝试的时刻、话题、结果和等待原因。配置范围与节奏规则见[主动私聊规格](../../docs/architecture/proactive-spec.md)。
