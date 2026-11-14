"""供插件作者离线核对备份提供者的最小生命周期契约。"""

import inspect

from clytie.sdk.backup import BackupRequest, BackupSink, BackupTicket, BackupView
from clytie.sdk.cancellation import CancelToken


async def assert_backup_provider_contract(
    provider,
    request: BackupRequest,
    sink: BackupSink,
    *,
    epoch: str = "test_epoch",
    expected_assets=None,
) -> None:
    """校验类型、版本、epoch 与幂等释放；调用者提供虚构材料和受控 sink。

    此检查不证明跨组件一致性或磁盘布局完整性；超时、取消及迟到资源跟踪
    必须另由宿主测试验证，不通过测试辅助器运行真实账号或模型。
    """
    for name, count in [("prepare", 2), ("capture", 3), ("materialize", 3), ("release", 1)]:
        method = getattr(provider, name, None)
        assert inspect.iscoroutinefunction(method), f"{name} must be async"
        inspect.signature(method).bind(*([None] * count))
    cancel = CancelToken()
    ticket = await provider.prepare(request, cancel)
    assert isinstance(ticket, BackupTicket), "prepare must return BackupTicket"
    try:
        view = await provider.capture(ticket, epoch, cancel)
        assert isinstance(view, BackupView), "capture must return BackupView"
        assert view.epoch == epoch, "capture epoch mismatch"
        assert view.data_version == request.data_version, "capture data_version mismatch"
        assets = tuple(asset.model_dump_json() for asset in view.assets)
        if expected_assets is not None:
            assert assets == tuple(asset.model_dump_json() for asset in expected_assets), (
                "declared asset dependencies do not match fixture"
            )
        assert await provider.materialize(view, sink, cancel) is None
        assert tuple(asset.model_dump_json() for asset in view.assets) == assets, (
            "materialize changed captured asset dependencies"
        )
    finally:
        assert await provider.release(ticket) is None
        assert await provider.release(ticket) is None
