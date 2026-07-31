# Linux 部署包双击入口设计

## 目标

在项目根目录新增 `package-linux.cmd`。Windows 用户双击该文件即可运行现有
`package-linux.ps1`，无需手工输入 PowerShell 命令。

## 结构

- `package-linux.cmd` 只负责启动、退出码传递和结果展示。
- `package-linux.ps1` 继续负责检查文件、排除缓存、创建和验证归档。
- `.cmd` 使用自身路径定位同目录的 PowerShell 脚本，不依赖用户当前工作目录。

## 执行流程

1. 检查同目录下是否存在 `package-linux.ps1`。
2. 使用 `powershell.exe -NoProfile -ExecutionPolicy Bypass -File` 调用该脚本。
3. 保存 PowerShell 进程的退出码。
4. 退出码为零时显示成功信息；否则显示失败信息和退出码。
5. 暂停窗口，让双击启动的用户能够阅读结果。
6. `.cmd` 使用原 PowerShell 退出码结束，便于命令行或自动化判断结果。

## 错误处理

- 缺少 `package-linux.ps1` 时不尝试打包，显示明确错误并以非零状态退出。
- PowerShell 打包失败时不掩盖错误；原错误输出保持可见。
- 不修改系统或当前用户的 PowerShell 执行策略。

## 验证

- 从项目根目录双击或执行 `.cmd` 均能生成固定名称的部署包。
- 从其他工作目录调用 `.cmd` 仍能正确找到 PowerShell 脚本。
- 临时缺少 PowerShell 脚本时，入口返回非零退出码。
- 正常运行时仍沿用 PowerShell 脚本的归档内容和排除规则。
