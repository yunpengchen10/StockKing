# 本地构建与分发

简体中文 | [English](LOCAL_BUILD.en.md) · [返回 README](../README.md)

Stock King 在自己的电脑构建，不使用 GitHub Actions、云服务器或付费构建平台。支持 Windows x64 与 Apple Silicon（macOS 15+）。下载开源依赖仍需要网络，本地运行会使用电脑的存储、内存和算力。

## 1. 普通用户安装

Mac 用户可直接解压维护者提供的 `Stock-King-macos-arm64.zip`，将 `Stock King.app` 移入“应用程序”并打开。界面左下角显示“已连接”表示桌面已连接到包内研究引擎；此安装方式无需 Python、Go、Node.js 或 Wails。

Windows 用户安装维护者提供的原生 wheel；它包含桌面程序和冻结后的研究引擎。安装 64 位 Python 3.11+（建议 3.12）后，在包所在目录运行：

```text
python -m pip install ./stock_king-2.6.0-py3-none-win_amd64.whl
stock-king
```

Apple Silicon 也可安装 `stock_king-2.6.0-py3-none-macosx_15_0_arm64.whl`，此时须使用原生 ARM64 Python 3.11+。Windows 需要微软 WebView2 Runtime。安装包版本号变化时使用实际文件名。

当前仓库仍是私有内测，尚未发布 PyPI 或公开下载页。源码读取需要仓库权限；已有包可以在自己的电脑安装。软件显示名为 **Stock King**，仓库名为 **StockKing**；`stock-king` 命令和 Python 的 `stock_king` 模块名保持兼容。

## 2. 源码研究工具

需要 Git、Python 3.11+（建议 3.12）以及当前私有仓库读取权限。先通过正常 GitHub 登录配置 Git 访问，不要把令牌写入安装命令。

```text
python -m pip install "stock-king[data] @ git+https://github.com/yunpengchen10/StockKing.git@main"
stock-king doctor
stock-king evidence 600519
```

`[data]` 用于 AKShare 证据研究；`[engine]` 加入本地研究 API；`[engine,models]` 加入 LightGBM/PyTorch 模型依赖。源码安装不含编译好的桌面，需单独安装原生 wheel 或自行构建。历史证据只能读取已有归档，不能把当前数据填回过去。

## 3. Windows x64 打包

安装原生 x64 Python 3.12、Node.js 20+ 和 Go 1.26（版本见 `desktop/go.mod`）。本项目的 Wails 2.11 绑定曾在 Go 1.27 上构建失败。使用已有源码目录，或通过 Git 获取：

```powershell
git clone https://github.com/yunpengchen10/StockKing.git
cd StockKing
powershell -ExecutionPolicy Bypass -File scripts/build-windows-wheel.ps1
```

脚本会创建或使用项目 `.venv`、安装依赖与 Wails 2.11、运行重点 Python/前端/Go 测试、冻结研究引擎、构建桌面，再打包并验证原生 wheel。不需要 NSIS 或购买代码签名证书。可用 `-Python C:\path\to\python.exe` 指定 Python；已有 `.venv` 也必须是 x64。

结果位于 `dist/desktop/*win_amd64.whl`，终端会输出 SHA-256。构建失败时先处理报错再重新运行，不要跳过失败步骤把包标为已验收。未签名应用可能显示系统信誉提示。

## 4. Apple Silicon 打包

使用 Apple Silicon Mac、macOS 15+ 和原生 ARM64 工具，避免通过 Rosetta 运行。需要 Git、Python 3.11 或 3.12、Node.js 20+、Go 1.26、Xcode Command Line Tools，以及 Homebrew 的 `libomp`。

先确认 Command Line Tools 已安装：运行 `xcode-select -p`；若报错，运行 `xcode-select --install`，等待系统安装完成后再继续。若 Homebrew 提示工具版本过旧，先通过 macOS“软件更新”升级。若没有 `brew` 命令，先按 [Homebrew 官方安装说明](https://brew.sh/)安装。然后从 [Python 官方 macOS 下载页](https://www.python.org/downloads/macos/)安装 universal2 版 Python 3.11 或 3.12（推荐 3.12）；已有可用的 ARM64 Python 可跳过。接着执行：

```bash
brew install node go@1.26 libomp
export PATH="$(brew --prefix go@1.26)/bin:$PATH"
go version  # 应为 go1.26.x
git clone https://github.com/yunpengchen10/StockKing.git
cd StockKing
bash scripts/build-macos.sh
```

构建脚本检查 Python 版本和架构，自动安装 Wails 2.11，并在现有 `.venv` 不兼容时重建。需要指定解释器时，可运行 `PYTHON_BIN=python3.11 bash scripts/build-macos.sh`。若依赖下载需要代理，请在执行脚本前设置实际可用的 `HTTP_PROXY`、`HTTPS_PROXY` 等环境变量；终端工具不一定继承系统代理设置。

首次构建会下载前端、Go 和 Python 依赖，并运行测试、冻结研究引擎、构建桌面、验证 wheel，因此可能耗时较长。构建脚本在非云同步的临时目录完成本地签名，避免 `Documents` 等目录产生的扩展属性破坏签名。产物是 `artifacts/macos/Stock-King-macos-arm64.zip` 和 `dist/desktop/*macosx_15_0_arm64.whl`。不需要付费 Apple 开发者账号；该方式不提供 Apple 公证，首次打开可能需要在“系统设置 → 隐私与安全性”中允许。用户应自行确认下载来源。

构建完成后，解压 ZIP，将 `Stock King.app` 移入“应用程序”并打开，检查界面左下角是否显示“已连接”。后台定时任务是独立的可选安装步骤，须在应用放入 `/Applications` 后从源码目录运行：

```bash
python3 scripts/install-macos-schedule.py
# 以后停用：python3 scripts/install-macos-schedule.py --remove
```

此脚本注册五个当前用户的 `launchd` 任务。保持电脑开机、唤醒并登录；工作日 09:10、10:20、14:45 是准备时间，分别在北京时间 09:20、10:30、14:55 复核并刷新。安装后可检查 `launchctl print "gui/$(id -u)/com.stockking.research.0920"`，任务日志在 `~/Library/Logs/Stock King/`。非计划时段显示未运行是正常的。

### 本机已遇到的构建问题

| 现象 | 处理方式 |
| --- | --- |
| `venv` 或 `ensurepip` 报 `pyexpat` 符号错误 | 某些 Homebrew Python 安装与当前系统组合下会出现该问题。改用 Python 官方 universal2 安装包，并以 `PYTHON_BIN=/usr/local/bin/python3.11` 等实际路径运行脚本。 |
| Wails 生成绑定时出现 `package "bytes" without types` | 使用 Go 1.26；脚本会选择 Go 1.26.8 工具链。确认 `go version` 和 `desktop/go.mod`，避免当前 Go 1.27。 |
| 下载 Go 或 Python 依赖时 DNS 超时 | 检查终端的 `HTTP_PROXY`、`HTTPS_PROXY` 或网络连接，确认代理服务确实可用后重试。 |
| `codesign` 报资源分叉或 Finder 信息 | 使用更新后的脚本；它会把待签名应用复制到系统临时目录，在那里签名与验证。 |
| 首次冻结引擎时看似停在模块导入检查 | 第一次导入 PyTorch 等大型模块可耗时数分钟。脚本会输出当前检查的模块并为首次导入预留更长时间；只有出现超时或错误时才需要排查。 |

## 5. 通用研究包检查

维护者可在任一支持的平台运行：

```text
python -m pip install build
python -m build
python scripts/smoke-wheel.py
```

这验证通用研究包，不代替原生桌面验收。Windows 和 Apple Silicon 的完整构建分别需要对应机器；不能用 Windows 结果声称完成 Mac 实机验证。

## 6. 扩大用户范围的发布方式

推荐逐步提供本地构建的两个平台安装包、SHA-256、中英文快速开始和版本说明。可以手动发布到 GitHub Releases，无需运行 Actions；是否公开仓库或发布 PyPI 由维护者后续决定。当前没有自动上传或公开动作。

发行包只包含程序、依赖与许可证，不包含个人数据库、API Key、行情归档、训练权重或通知收件人。保留 GPL-3.0 及第三方声明。给新用户的默认流程是免费行情 → 自选 → 本地精选 → 图表核验；AI 可选本地 Ollama 或导入报告，无需付费 API。

私有阶段只有获得授权的用户可以访问源码；面向所有用户的公开推广需要另行准备可公开访问的源码与发行入口。
