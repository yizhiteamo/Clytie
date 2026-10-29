# desktop-notifier

Windows Toast 通知参考插件，需要 `subprocess` 权限。固定 PowerShell 命令使用环境变量读取通知标题和正文，再通过 XML 文本节点赋值；通知内容不进入命令字符串。结果无法确认时返回 unknown。

A2 仅注入假子进程后端做契约和命令注入回归测试，不真正执行 PowerShell 或弹窗。第一次真实通知必须另行取得用户同意；实际 Windows 通知权限与应用标识配置尚未实机验证。
