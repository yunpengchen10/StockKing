# Windows 安装与启动修复

[English](WINDOWS_INSTALL.en.md)

## v2.6.1 一键安装包

文件名：`Stock-King-Setup-x64-v2.6.1.exe`。适用于 Windows 10/11 x64，包含桌面程序、StockKing V1.1 本地研究引擎及离线 WebView2 安装程序，无需另外安装 Python、Go 或 Node.js。仅在缺少 WebView2 时安装该组件；行情下载仍需要网络。

1. 关闭正在运行的 Stock King，再双击安装包。
2. 已有安装时选择原来的安装目录，例如 `D:\Stock King`。新用户可使用默认目录。
3. 完成后勾选“Open Stock King”，或双击桌面快捷方式。
4. 确认窗口出现、引擎连接成功，再检查“精选 → 推荐记录 / 延后复盘 / 学习状态”。

安装不会把个人数据库、密钥、自选或训练模型装入发行包。已有数据存放在 `%APPDATA%\Stock King` 与 `%LOCALAPPDATA%\Stock King`；卸载程序保留这些用户数据。升级前建议备份这两个目录。安装器注册七个当前用户后台任务；自动推荐与学习仍分别由软件开关控制，电脑需保持开机并登录。

安装包附 `SHA256SUMS.txt`。可用 `Get-FileHash .\Stock-King-Setup-x64-v2.6.1.exe -Algorithm SHA256` 核对。Stock King 程序与安装包未购买代码签名证书，内置 WebView2 安装程序已验证微软签名。

## 修复了什么

旧的本地更新脚本会以隐藏窗口方式启动桌面程序，重复打开时只提示“已运行”。v2.6.1 改为显示窗口，并在更新完成前检查可见窗口；再次点击快捷方式会恢复并聚焦已有窗口。

只看到后台进程时，可在任务管理器结束 **Stock King.exe** 后重新打开。不要删除用户数据目录，也不要使用旧版安装包覆盖新引擎。

## 维护者打包

先构建并验证桌面与冻结引擎，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows-installer.ps1 `
  -BuiltExe 'desktop/build/bin/Stock King.exe' `
  -BuiltEngineDir 'daily-engine/dist/backend/stock_analysis' `
  -WebViewInstaller 'desktop/build/windows/installer/tmp/MicrosoftEdgeWebView2RuntimeInstallerX64.exe'
```

输出在 `dist/installers`。脚本检查版本、微软签名及载荷中的数据库/配置文件，并生成安装包和逐文件哈希清单。验收可用安装器的 `/S /EXTRACTONLY /D=绝对目录` 模式只解包，核对载荷；该模式不注册任务、快捷方式或卸载信息，也不启动程序、不安装 WebView2。应用运行验收应使用隔离的数据目录。
