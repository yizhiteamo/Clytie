# A4.3 本机 Windows 虚构规模性能记录

Windows-05；仅组件性能证据，不是独立验收结论或真实实例速度预测。完整原始数据保留本地，以下摘要保留失败标志、测量条件及精度限制。


# 离线备份性能摘要

显示精度：秒、毫秒、百分比及 MiB 保留三位小数（四舍五入）；1 MiB = 2^20 bytes。舍入仅用于显示，超界标记根据未舍入值计算。
原始 JSON 保留在本地；本摘要仍须通过通用与私有扫描才可发布。数据不是性能通过结论，未测量不代表通过。

## 机器与运行条件

- os: Windows
- release: 10
- machine: AMD64
- python: 3.10.6
- sqlite: 3.37.2
- logical_cpus: 12
- processor: Intel64 Family 6 Model 151 Stepping 5, GenuineIntel
- script_sha256: ebf99f83bb9649f5ea9e586c7824ac103d5828cc54fe772a4b70ce1c560c4ea1
- seed: 0x11da9
- conditions: same process; sequential baseline/backup; warm OS cache; no cache flush
- background_load: Sequential after final targeted regressions; no concurrent project tests; unrelated host load unknown

## small_text / 无备份

规模：数据库 128 行（每行 4 KiB）；不可变文件 8 个，每文件 0.001 MiB。
固定请求数：800；间隔：0.025 s；备份重叠样本：0。
备份期间负载窗口覆盖率：未测量；覆盖时长：未测量。覆盖率按首个计划请求至最后完成请求的时间窗与备份时间段交集计算，不是持续占用 CPU 的比例；低于 100% 表示部分备份阶段无负载覆盖。
负载未覆盖完整备份（按未舍入值）：未测量。

| 指标 (ms) | 样本数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 响应 | 800 | 8.831 | 10.376 | 11.868 | 13.574 |
| 写入等待 | 800 | 5.320 | 6.273 | 6.531 | 9.151 |
| 备份重叠响应 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |
| 捕获门闩持有 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |
| 捕获门闩获取等待 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |

- 捕获持有超过 250 ms：false
- 捕获等待超过 2000 ms：false
- RSS 峰值：51.270 MiB
- 临时盘峰值：0.000 MiB
- WAL 峰值：4.051 MiB
- 备份：未运行
- 负载错误数：0

## small_text / 有备份

规模：数据库 128 行（每行 4 KiB）；不可变文件 8 个，每文件 0.001 MiB。
固定请求数：800；间隔：0.025 s；备份重叠样本：37。
备份期间负载窗口覆盖率：100.000%；覆盖时长：0.900 s。覆盖率按首个计划请求至最后完成请求的时间窗与备份时间段交集计算，不是持续占用 CPU 的比例；低于 100% 表示部分备份阶段无负载覆盖。
负载未覆盖完整备份（按未舍入值）：false。

| 指标 (ms) | 样本数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 响应 | 800 | 8.581 | 11.414 | 46.709 | 131.984 |
| 写入等待 | 800 | 5.331 | 6.326 | 6.910 | 40.615 |
| 备份重叠响应 | 37 | 11.742 | 114.528 | 131.984 | 131.984 |
| 捕获门闩持有 | 1 | 2.884 | 2.884 | 2.884 | 2.884 |
| 捕获门闩获取等待 | 1 | 4.108 | 4.108 | 4.108 | 4.108 |

- 捕获持有超过 250 ms：false
- 捕获等待超过 2000 ms：false
- RSS 峰值：55.039 MiB
- 临时盘峰值：2.762 MiB
- WAL 峰值：4.051 MiB
- 备份耗时：0.900 s
- 备份完整性：complete
- 包大小：0.528 MiB；核验文件数：14
- 缺项数：0
- 负载错误数：0

## large_database / 无备份

规模：数据库 32.768 千行（每行 4 KiB）；不可变文件 8 个，每文件 0.001 MiB。
固定请求数：800；间隔：0.025 s；备份重叠样本：0。
备份期间负载窗口覆盖率：未测量；覆盖时长：未测量。覆盖率按首个计划请求至最后完成请求的时间窗与备份时间段交集计算，不是持续占用 CPU 的比例；低于 100% 表示部分备份阶段无负载覆盖。
负载未覆盖完整备份（按未舍入值）：未测量。

| 指标 (ms) | 样本数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 响应 | 800 | 8.911 | 10.885 | 12.030 | 14.452 |
| 写入等待 | 800 | 5.136 | 6.264 | 6.478 | 9.084 |
| 备份重叠响应 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |
| 捕获门闩持有 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |
| 捕获门闩获取等待 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |

- 捕获持有超过 250 ms：false
- 捕获等待超过 2000 ms：false
- RSS 峰值：56.824 MiB
- 临时盘峰值：0.000 MiB
- WAL 峰值：4.051 MiB
- 备份：未运行
- 负载错误数：0

## large_database / 有备份

规模：数据库 32.768 千行（每行 4 KiB）；不可变文件 8 个，每文件 0.001 MiB。
固定请求数：800；间隔：0.025 s；备份重叠样本：340。
备份期间负载窗口覆盖率：100.000%；覆盖时长：8.484 s。覆盖率按首个计划请求至最后完成请求的时间窗与备份时间段交集计算，不是持续占用 CPU 的比例；低于 100% 表示部分备份阶段无负载覆盖。
负载未覆盖完整备份（按未舍入值）：false。

| 指标 (ms) | 样本数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 响应 | 800 | 9.030 | 12.467 | 32.078 | 111.155 |
| 写入等待 | 800 | 5.577 | 7.261 | 10.081 | 19.776 |
| 备份重叠响应 | 340 | 9.332 | 14.380 | 64.128 | 111.155 |
| 捕获门闩持有 | 1 | 3.879 | 3.879 | 3.879 | 3.879 |
| 捕获门闩获取等待 | 1 | 4.426 | 4.426 | 4.426 | 4.426 |

- 捕获持有超过 250 ms：false
- 捕获等待超过 2000 ms：false
- RSS 峰值：62.977 MiB
- 临时盘峰值：561.739 MiB
- WAL 峰值：4.051 MiB
- 备份耗时：8.484 s
- 备份完整性：complete
- 包大小：129.075 MiB；核验文件数：14
- 缺项数：0
- 负载错误数：0

## large_asset / 无备份

规模：数据库 128 行（每行 4 KiB）；不可变文件 1 个，每文件 128.000 MiB。
固定请求数：800；间隔：0.025 s；备份重叠样本：0。
备份期间负载窗口覆盖率：未测量；覆盖时长：未测量。覆盖率按首个计划请求至最后完成请求的时间窗与备份时间段交集计算，不是持续占用 CPU 的比例；低于 100% 表示部分备份阶段无负载覆盖。
负载未覆盖完整备份（按未舍入值）：未测量。

| 指标 (ms) | 样本数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 响应 | 800 | 8.984 | 11.125 | 12.200 | 14.403 |
| 写入等待 | 800 | 5.061 | 6.245 | 6.504 | 9.473 |
| 备份重叠响应 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |
| 捕获门闩持有 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |
| 捕获门闩获取等待 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |

- 捕获持有超过 250 ms：false
- 捕获等待超过 2000 ms：false
- RSS 峰值：55.621 MiB
- 临时盘峰值：0.000 MiB
- WAL 峰值：4.051 MiB
- 备份：未运行
- 负载错误数：0

## large_asset / 有备份

规模：数据库 128 行（每行 4 KiB）；不可变文件 1 个，每文件 128.000 MiB。
固定请求数：800；间隔：0.025 s；备份重叠样本：226。
备份期间负载窗口覆盖率：100.000%；覆盖时长：5.646 s。覆盖率按首个计划请求至最后完成请求的时间窗与备份时间段交集计算，不是持续占用 CPU 的比例；低于 100% 表示部分备份阶段无负载覆盖。
负载未覆盖完整备份（按未舍入值）：false。

| 指标 (ms) | 样本数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 响应 | 800 | 8.553 | 11.461 | 34.587 | 101.713 |
| 写入等待 | 800 | 5.237 | 6.392 | 8.019 | 15.414 |
| 备份重叠响应 | 226 | 8.725 | 20.900 | 69.700 | 101.713 |
| 捕获门闩持有 | 1 | 7.104 | 7.104 | 7.104 | 7.104 |
| 捕获门闩获取等待 | 1 | 4.388 | 4.388 | 4.388 | 4.388 |

- 捕获持有超过 250 ms：false
- 捕获等待超过 2000 ms：false
- RSS 峰值：62.020 MiB
- 临时盘峰值：386.778 MiB
- WAL 峰值：4.051 MiB
- 备份耗时：5.646 s
- 备份完整性：complete
- 包大小：128.559 MiB；核验文件数：8
- 缺项数：0
- 负载错误数：0

## 测量边界

- Component latency: fixed-rate query + coordinated metadata commit + zero-delay FakeModel.
- Real capture_online/capture_group/source/container pack+unpack; no API/network/model.
- Large asset uses production core ownership registry, BackupView dependency and streaming verification.
- RSS current working set sampled every >=50ms; short peaks may be missed.
- Temporary logical file sizes sampled plus final exact sum; excludes filesystem metadata.
- Sampler walks temporary/source trees; observer I/O overhead is included, not subtracted.
- Samples include scheduling delay (no coordinated omission); tails retain outliers.
- MockClock advanced by benchmark driver; timings use perf_counter only.
- Capture gate body/wait observed with wall clock; original policy/checkpoints unchanged.
- No performance pass threshold; full backup may outlast fixed request window.


# 离线备份性能摘要

显示精度：秒、毫秒、百分比及 MiB 保留三位小数（四舍五入）；1 MiB = 2^20 bytes。舍入仅用于显示，超界标记根据未舍入值计算。
原始 JSON 保留在本地；本摘要仍须通过通用与私有扫描才可发布。数据不是性能通过结论，未测量不代表通过。

## 机器与运行条件

- os: Windows
- release: 10
- machine: AMD64
- python: 3.10.6
- sqlite: 3.37.2
- logical_cpus: 12
- processor: Intel64 Family 6 Model 151 Stepping 5, GenuineIntel
- script_sha256: ebf99f83bb9649f5ea9e586c7824ac103d5828cc54fe772a4b70ce1c560c4ea1
- seed: 0x11da9
- conditions: same process; sequential baseline/backup; warm OS cache; no cache flush
- background_load: Sequential after standard cases; no concurrent project tests; unrelated host load unknown

## many_small_files / 无备份

规模：数据库 128 行（每行 4 KiB）；不可变文件 2048 个，每文件 0.001 MiB。
固定请求数：4800；间隔：0.025 s；备份重叠样本：0。
备份期间负载窗口覆盖率：未测量；覆盖时长：未测量。覆盖率按首个计划请求至最后完成请求的时间窗与备份时间段交集计算，不是持续占用 CPU 的比例；低于 100% 表示部分备份阶段无负载覆盖。
负载未覆盖完整备份（按未舍入值）：未测量。

| 指标 (ms) | 样本数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 响应 | 4800 | 8.734 | 10.814 | 11.947 | 14.906 |
| 写入等待 | 4800 | 5.336 | 6.378 | 6.796 | 9.530 |
| 备份重叠响应 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |
| 捕获门闩持有 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |
| 捕获门闩获取等待 | 未测量 | 未测量 | 未测量 | 未测量 | 未测量 |

- 捕获持有超过 250 ms：false
- 捕获等待超过 2000 ms：false
- RSS 峰值：56.727 MiB
- 临时盘峰值：0.000 MiB
- WAL 峰值：4.051 MiB
- 备份：未运行
- 负载错误数：0

## many_small_files / 有备份

规模：数据库 128 行（每行 4 KiB）；不可变文件 2048 个，每文件 0.001 MiB。
固定请求数：4800；间隔：0.025 s；备份重叠样本：3860。
备份期间负载窗口覆盖率：100.000%；覆盖时长：96.485 s。覆盖率按首个计划请求至最后完成请求的时间窗与备份时间段交集计算，不是持续占用 CPU 的比例；低于 100% 表示部分备份阶段无负载覆盖。
负载未覆盖完整备份（按未舍入值）：false。

| 指标 (ms) | 样本数 | P50 | P95 | P99 | 最大值 |
|---|---:|---:|---:|---:|---:|
| 响应 | 4800 | 9.229 | 17.024 | 20.027 | 122.854 |
| 写入等待 | 4800 | 5.468 | 6.528 | 7.473 | 80.560 |
| 备份重叠响应 | 3860 | 9.397 | 17.272 | 20.945 | 122.854 |
| 捕获门闩持有 | 1 | 2.964 | 2.964 | 2.964 | 2.964 |
| 捕获门闩获取等待 | 1 | 5.911 | 5.911 | 5.911 | 5.911 |

- 捕获持有超过 250 ms：false
- 捕获等待超过 2000 ms：false
- RSS 峰值：65.617 MiB
- 临时盘峰值：9.939 MiB
- WAL 峰值：4.051 MiB
- 备份耗时：96.485 s
- 备份完整性：complete
- 包大小：3.720 MiB；核验文件数：2054
- 缺项数：0
- 负载错误数：0

## 测量边界

- Component latency: fixed-rate query + coordinated metadata commit + zero-delay FakeModel.
- Real capture_online/capture_group/source/container pack+unpack; no API/network/model.
- Large asset uses production core ownership registry, BackupView dependency and streaming verification.
- RSS current working set sampled every >=50ms; short peaks may be missed.
- Temporary logical file sizes sampled plus final exact sum; excludes filesystem metadata.
- Sampler walks temporary/source trees; observer I/O overhead is included, not subtracted.
- Samples include scheduling delay (no coordinated omission); tails retain outliers.
- MockClock advanced by benchmark driver; timings use perf_counter only.
- Capture gate body/wait observed with wall clock; original policy/checkpoints unchanged.
- No performance pass threshold; full backup may outlast fixed request window.
