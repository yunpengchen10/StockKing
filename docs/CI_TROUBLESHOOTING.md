# 构建检查排查

简体中文 | [English](CI_TROUBLESHOOTING.en.md) · [返回 README](../README.md)

## 检查几秒内全部失败

先打开具体 Actions 运行的 **Summary → Annotations**。2026-09-24，提交 `89e55d1` 的 Windows desktop wheel、macOS 与 Python package（Windows/macOS 两项）共四个检查没有启动。GitHub 注释指出账号近期付款失败或需要提高支出限额；因此没有实际编译/测试步骤日志。

这是 GitHub 托管运行器的账号计费限制，不能据此判断软件编译失败，也不能把这些检查标成通过。检查依据见 [该次 Python package 运行](https://github.com/yunpengchen10/stock-king/actions/runs/35971302887)（私有仓库需要访问权限）。注释未区分究竟是付款问题还是支出限制，需账号所有者核对。

1. 在 GitHub 账号 **Settings → Billing & licensing**（部分界面称 Billing & plans）查看付款状态、Actions 用量与预算限制。
2. 由账号所有者决定如何处理付款、额度或预算；无需为此公开仓库，也不应把关闭检查当成修复。
3. 限制解除后，在最新代码对应的三个工作流中选择 **Re-run failed jobs / Re-run all jobs**，或通过 **Run workflow** 对 `main` 手动运行。Python package 包含两个平台任务，所以合计四项检查。
4. 确认步骤真正执行、检查通过、产物存在，再下载对应平台包。若之后出现源码/依赖错误，应按该次实际日志另行排查。

私有仓库的托管运行器用量及存储可能涉及账号额度与收费；具体规则见 [GitHub Actions 计费说明](https://docs.github.com/en/billing/concepts/product-billing/github-actions)。不要在账单限制尚未解除时反复推送或重跑。

## 本仓库的构建范围

| 工作流 | 目标与结果 |
| --- | --- |
| Windows desktop wheel | Windows x64 原生桌面 wheel 与安装验证。 |
| macOS | Apple Silicon、macOS 15+ 的应用与原生 wheel；不构建 Intel Mac。 |
| Python package | Windows 和 macOS 分别检查研究包安装与测试；通用研究包不等同于完整原生桌面包。 |

本次工作流调整保留代码构建和测试，减少重复使用：

- `main` 的代码/构建配置修改继续触发检查；纯 `README*.md` 或 `docs/` 修改跳过这三个打包工作流。`workflow_dispatch` 保留，仍可手动运行。
- 同一工作流、同一分支的新运行会取消旧运行，避免重复构建堆积。
- 新上传产物保留 **14 天**。该设置不会追溯修改已经上传的历史产物，也不会立即解除已有账单限制。
- 同时修改工作流配置与文档的提交仍触发 CI。跳过文档构建不等于某次代码检查已通过。

路径筛选规则参考 [GitHub 工作流触发说明](https://docs.github.com/en/actions/how-tos/write-workflows/choose-when-workflows-run/trigger-a-workflow)。若以后把这些工作流设为 PR 必需检查，需同时处理路径跳过可能造成的等待状态。

## 安装失败与构建失败的区别

`unsupported wheel` 通常先核对包文件名、操作系统、Python 架构及 macOS 最低版本；它不等于 GitHub 构建失败。`expired artifact` 表示下载产物已过保留期，需要重建或使用已有下载副本。实际依赖安装、编译、测试失败则应查看对应步骤日志，不要用旧提交的成功结果替代当前提交验收。
