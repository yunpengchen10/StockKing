# Windows 安装与修复

[English](WINDOWS_INSTALL.en.md)

## v2.6.3 一键安装包

文件名：`Stock-King-Setup-x64-v2.6.3.exe`。适用于 Windows 10/11 x64，包含桌面程序、本地研究引擎及离线 WebView2 安装程序，无需另外安装 Python、Go 或 Node.js。仅在缺少 WebView2 时安装该组件；行情下载仍需要网络。

1. 关闭正在运行的 Stock King，再双击安装包。
2. 已有安装时选择原来的安装目录，例如 `D:\Stock King`。新用户可使用默认目录。
3. 完成后勾选“Open Stock King”，或双击桌面快捷方式。
4. 确认窗口出现、引擎连接成功，再检查“精选 → 推荐记录 / 延后复盘 / 学习状态”。

安装不会把个人数据库、密钥、自选或训练模型装入发行包。已有数据存放在 `%APPDATA%\Stock King` 与 `%LOCALAPPDATA%\Stock King`；卸载程序保留这些用户数据。升级前建议备份这两个目录。安装器注册七个当前用户后台任务；自动推荐与学习仍分别由软件开关控制，电脑需保持开机并登录。

安装包附 `SHA256SUMS.txt`。可用 `Get-FileHash .\Stock-King-Setup-x64-v2.6.3.exe -Algorithm SHA256` 核对。Stock King 程序与安装包未购买代码签名证书，内置 WebView2 安装程序已验证微软签名。

## 修复了什么

v2.6.3 修复分钟历史分页提前停止、短响应阻止备用源补齐、20日基准未齐就停止回补，以及报价合并丢失涨跌停价的问题；优化财报分页、行业缓存读取和报价时效。延后复盘先保存已有结果，再补充所需数据并重新结算，价格路径观测与有效训练样本分别统计。关键数据不足或冲突时，正式候选保持受限，不将缺失值补成零。

新安装会按自己的研究股票下载和积累数据，不包含打包者的历史记录、持仓或模型。首次运行及历史不足时仍可能显示数据缺口；外部行情源缺失或矛盾的数据不能凭空补齐。升级保留历史推荐快照，不用新取得的数据冒充当时已知的信息。

v2.6.2 修复推荐记录响应超过 16 MiB 时被截断的问题。此前会出现 `unexpected end of JSON input` 并显示“记录暂不可用”；更新后可完整读取和显示记录，保留原有信号数据。

旧的本地更新脚本会以隐藏窗口方式启动桌面程序，重复打开时只提示“已运行”。v2.6.1 改为显示窗口，并在更新完成前检查可见窗口；再次点击快捷方式会恢复并聚焦已有窗口。

只看到后台进程时，可在任务管理器结束 **Stock King.exe** 后重新打开。不要删除用户数据目录，也不要使用旧版安装包覆盖新引擎。

## 维护者打包

先构建并验证桌面与冻结引擎，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows-installer.ps1 `
  -BuiltExe 'desktop/build/bin/Stock King.exe' `
  -BuiltEngineDir 'daily-engine/dist/stock_analysis' `
  -WebViewInstaller 'desktop/build/windows/installer/tmp/MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
```

输出在 `dist/installers`。脚本检查版本、微软签名及载荷中的数据库/配置文件，并生成安装包和逐文件哈希清单。验收可用安装器的 `/S /EXTRACTONLY /D=绝对目录` 模式只解包，核对载荷；该模式不注册任务、快捷方式或卸载信息，也不启动程序、不安装 WebView2。应用运行验收应使用隔离的数据目录。
