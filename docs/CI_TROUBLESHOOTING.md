# 历史构建检查说明

简体中文 | [English](CI_TROUBLESHOOTING.en.md) · [返回 README](../README.md)

## 当前方案：本地构建

项目已停用 GitHub Actions，并移除三个云端构建工作流。开发、测试、打包与安装校验在自己的电脑完成，不要求开通 GitHub 付费额度、购买云服务器或使用付费构建服务。详细步骤见 [本地构建与分发](LOCAL_BUILD.md)。

本地 Windows 脚本为 `scripts/build-windows-wheel.ps1`，Apple Silicon 脚本为 `scripts/build-macos.sh`。普通用户安装已经构建好的原生 wheel 时无需安装 Go、Node.js 或 Wails；源码研究 CLI 则可直接通过 pip 安装。

## 旧提交为何有四项失败

2026-09-24，提交 `89e55d1` 的 Windows desktop wheel、macOS 与 Python package（Windows/macOS 两项）没有启动。GitHub 的注释指出账号付款失败或支出限制；没有实际编译/测试步骤日志。具体是哪一种账单问题未作核定。该次运行属于旧仓库，运行 ID 为 `35971302887`；旧仓库删除后无法再查看该记录。

现在不再依赖这条构建链路，不需要为软件使用或发布购买 Actions 额度。旧失败记录可能仍出现在提交历史中；停用服务不会把旧检查变成通过，也不会清除历史用量或已有账单。

## 当前版本如何验收

本地构建脚本会在测试、冻结引擎、桌面编译或原生 wheel 安装校验失败时停止。Windows x64 与 Apple Silicon 应分别在对应系统验收。源码包校验和原生桌面校验是不同层次；不要用一个平台的通过结果代替另一个平台。

`unsupported wheel` 先核对包名、系统架构、Python 架构和 macOS 15+ 要求。实际依赖、编译、测试错误则查看本地终端日志。没有可用安装包时，请本地重建或获取维护者提供的包，不依赖 Actions 临时产物。
