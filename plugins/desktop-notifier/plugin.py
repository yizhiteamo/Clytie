"""Windows 桌面通知；只通过注入的子进程能力执行固定命令。"""

import asyncio
from uuid import uuid4

from clytie.sdk.types import Receipt

POWERSHELL_APP_ID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"

SCRIPT = """
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$xml = [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime]::new()
$xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/></binding></visual></toast>')
$texts = $xml.GetElementsByTagName('text')
$texts.Item(0).InnerText = $env:CLYTIE_NOTICE_TITLE
$texts.Item(1).InnerText = $env:CLYTIE_NOTICE_BODY
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($env:CLYTIE_NOTICE_APP_ID).Show($toast)
"""


class DesktopNotifier:
    def __init__(self, ctx):
        self.ctx = ctx

    async def notify(self, notice, cancel):
        cancel.raise_if_cancelled()
        operation = asyncio.create_task(
            self.ctx.subprocess.run(
                ["powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", SCRIPT],
                timeout_s=5,
                env={
                    "CLYTIE_NOTICE_APP_ID": self.ctx.config.get("app_id", POWERSHELL_APP_ID),
                    "CLYTIE_NOTICE_TITLE": notice.title,
                    "CLYTIE_NOTICE_BODY": notice.body,
                },
            )
        )
        cancelled = asyncio.create_task(cancel.wait())
        try:
            await asyncio.wait((operation, cancelled), return_when=asyncio.FIRST_COMPLETED)
            cancel.raise_if_cancelled()
            result = await operation
            return Receipt(
                intent_id=uuid4().hex,
                status="sent" if result.returncode == 0 else "unknown",
                at=self.ctx.clock.now(),
            )
        finally:
            for task in (operation, cancelled):
                if not task.done():
                    task.cancel()
            await asyncio.gather(operation, cancelled, return_exceptions=True)


class Plugin:
    async def start(self, ctx):
        self.ctx = ctx
        await ctx.clock.sleep(0)

    def create_notifier(self):
        return DesktopNotifier(self.ctx)

    async def stop(self):
        await self.ctx.clock.sleep(0)
        self.ctx = None
