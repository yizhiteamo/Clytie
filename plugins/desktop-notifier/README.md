# Windows 桌面通知

`desktop-notifier` 使用 Windows PowerShell 在本机桌面显示 Clytie 提醒。将插件加入实例配置 `CLYTIE_HOME/clytie.local.yaml` 的 `notify.plugins` 列表即可启用。

```yaml
notify:
  plugins:
    - desktop-notifier
```

默认使用 Windows PowerShell 已注册的应用标识。守护进程通知的来源显示为“Windows PowerShell”，标题为“Clytie”。需要使用其他来源时，可用 `Get-StartApps` 查看本机已注册的 AppID，将所选值填入实例配置的 `plugins.desktop-notifier.config.app_id`。未注册的标识可能导致命令成功执行而通知没有显示。

首次弹窗前须取得用户同意。启用后可运行 `clytie supervisor notify-test` 发一条测试通知。插件返回 `sent` 只表示 PowerShell 命令执行成功，看到桌面上的测试通知才算验证通过。

插件需要 `subprocess` 权限。应用标识、标题和正文都通过环境变量传给固定命令，标题和正文写入 XML 文本节点。离线测试使用假子进程。
