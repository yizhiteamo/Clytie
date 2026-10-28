# ADR 0003：使用 uv 管理依赖与构建

日期：2026-10-28
状态：已采纳（实现选型）

## 决定

使用 `pyproject.toml` 声明运行依赖与开发依赖，提交 `uv.lock`，开发与 CI 使用 `uv sync --locked`。uv 工具版本固定为 `0.9.5`；构建后端 Hatchling 固定为 `1.33.0`，同时纳入开发锁文件，构建使用 `--no-build-isolation` 复用已锁定的依赖。包使用 src 布局。Python 下限是本机实际可用的 3.10 系列（本机为 3.10.6）。

## 理由与影响

锁文件保留传递依赖版本、来源和校验值，Windows 与 Linux 使用同一份解析结果。uv 只在开发环境安装依赖，不引入运行时守护进程或系统任务。依赖安装需要网络；应用状态检查与 pytest 使用离线假实现。

选择 PyYAML 解析配置，jsonschema 验证 draft 2020-12，packaging 处理声明的 PEP 440 语法，tzdata 为 Windows 提供 IANA 时区。pytest 与 pytest-asyncio 执行异步契约测试，ruff 检查风格及禁止的时间 API。Schema 随 wheel 打包，避免仅源码目录能运行的隐式依赖。
