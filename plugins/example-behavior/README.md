# example-behavior

行为、上下文段落、定时和服务扩展的最小示例。`quiet` 配置改变 before_attention，`text` 提供状态段落。手动 Tick 更新私有计数，通过 `example.state` 服务查询；不运行系统定时任务。支持由宿主 stop/start 重建的配置热加载。
