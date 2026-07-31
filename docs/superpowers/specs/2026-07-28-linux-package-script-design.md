# Linux 部署包脚本设计

## 目标

在项目根目录提供 `package-linux.ps1`。用户每次运行该脚本即可生成固定名称
`connection-topology-linux.tar.gz`，用于将项目迁移到内网 Linux 部署。

## 输入与输出

- 脚本从自身所在目录识别项目根目录，不依赖调用时的当前目录。
- 输出文件固定为项目根目录下的 `connection-topology-linux.tar.gz`。
- 如果旧输出文件存在，脚本先删除旧文件，再生成新包。

## 打包内容

必须包含：

- `app/`
- `pyproject.toml`
- `.env`
- `.env.example`
- `README.md`

可选包含：

- `wheelhouse/`：目录存在时加入部署包；不存在时显示提示并继续打包。

## 排除内容

即使排除项位于 `app/` 或 `wheelhouse/` 内，也不得写入压缩包：

- `__pycache__/`
- `*.pyc`
- `*.pyo`
- `*.log`
- `.pytest_cache/`
- `.ruff_cache/`

## 校验与错误处理

1. 开始前检查系统能否调用 `tar`。
2. 检查所有必须打包的路径；任何一项缺失时停止并列出缺失项。
3. 生成压缩包后检查 `tar` 的退出状态以及输出文件是否存在。
4. 失败时删除可能生成的不完整压缩包，并以非零状态退出。
5. 成功时输出压缩包绝对路径、文件大小以及是否包含 `wheelhouse/`。

## 安全边界

- 脚本不会修改项目源码或 `.env`。
- 不打包现有的 `connection_topology.db`。Linux 首次启动时由应用自动创建空数据库。
- 输出包包含带有 `APP_SECRET_KEY` 的 `.env`，成功信息需提醒用户安全传输和保存。

## 验证

- 正常执行后使用 `tar -tzf connection-topology-linux.tar.gz` 验证包可读取。
- 确认所有必须路径均存在。
- 确认缓存、字节码和日志没有出现在归档列表中。
- 分别验证 `wheelhouse/` 存在和不存在时的行为。
- 验证缺少必须文件时脚本失败且不会留下不完整压缩包。
