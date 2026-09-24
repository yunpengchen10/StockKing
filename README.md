# Stock King

<img src="branding/stock-king-logo.png" alt="Stock King" width="140" />

简体中文 | [English](README.en.md)

本地股票研究桌面软件：行情、K 线、自选、定时精选、复盘和本地模型学习。

- 腾讯、新浪免费行情互为备用，保留真实报价时间；过期或缺失数据不会冒充可执行推荐。
- 工作日 09:20、10:30、14:55 筛选，15:30 复盘，周五 15:45 学习，均按北京时间。
- 自动任务使用本地模型；AI 复审由你手动触发，不增加自动 AI 调用费用。
- 数据、模型和设置保存在本机。软件不自动下单。

需要让 AI 工具复用同一套行情，可使用 [只读 MCP 行情服务](quote-service/README.md)。支持 Windows、macOS 的 Python 运行方式；ChatGPT 云端仍需完成远程连接授权和实测。

## 推荐算法

2.6 新增三档入场检查与 AKShare 财报、业绩预告、解禁归档：稳健、均衡、激进在“当日机会”中切换。数据缺失不等于无风险，压力空间不足会阻止入选。初始阈值尚未获得样本外收益验证；详见 [算法对照、三档门槛与验证计划](docs/precision-and-packaging.md)。

当前日常精选采用“全市场线索 → 分钟价量与历史结构核验 → 三档入场检查 → 条件观察”的流程，每档最多 5 只，不补齐名额。证据规则版本 `king-evidence-20260920`，入场检查版本 `king-precision-v1`。这是本地、未经收益校准的证据规则。

| 环节 | 实际实现 |
| --- | --- |
| 股票池与初筛 | 主板、非 ST；成交额、量比、换手和开盘修复各自名次用于安排最多30只深研。取消涨幅40%加权总分；没有固定涨幅区间，也不把未深研股票称为已排除。 |
| 入选依据 | 盘中核验3/5分钟回升与VWAP承接或局部突破；盘前只列观察。按即时价量证据完整性、累计成交额安排观察；价格乖离、ATR距离、空间风险比和财务事件按三档检查，不显示未经校准的胜率。 |
| 指标与理由 | 每只保存实际入选原因、风险、触发/失效/不追条件，以及指标数值、单位、用途、来源时间和判断口径。指标包括1/3/5分钟涨速、VWAP、3分钟成交额、均线、ATR、波动、回撤及历史压力。缺失数据不补造。 |
| 能力缺口 | 分钟历史逐日缓存并尝试补齐20日同刻基准；历史、行业分钟或资金覆盖不足时不入选。财报与事件首次归档之前的历史可知数据不可回填；催化和预期差仍需原始新闻证据。 |
| 行情与风险 | 腾讯、新浪轮换与逐股回退；核对证券代码、来源时间、价格和新鲜度。09:20 竞价字段不足时只给条件观察，涨停等不可买对象单列风向标。 |
| 复盘与学习 | 保存候选和源时间，计算实际观察价格变化；缺数据保留未知。重复缺口成为下次核验提醒；周五仅在用户已选训练股票池内重训。观察涨跌不伪装成成交收益。 |
| 手动 AI 复审 | 汇总证据、风险和分歧，不改变本地排名；只有手动触发才调用所选模型。后台不会因本次调整增加AI调用。 |

## 量化算法

| 模块 | 算法与用途 |
| --- | --- |
| 量价特征 | Alpha158 风格的收益、动量、波动、价量关系和 K 线形态特征；只使用当时及此前的数据。 |
| BalancedRank（常规） | LightGBM LambdaRank、DoubleEnsemble 式难样本重加权及 Ridge 基线，研究 5 / 20 日横截面排序；结合暴露中性化与样本外非负组合权重。日常精选只取已合格的 5 日分支。 |
| SafeBound（保守） | LightGBM 分位数回归估计 20 / 60 日收益下界，配合样本外分位数校正、波动及回撤预测；使用 LightGBM、滚动波动率 / HAR 风格基线作比较。 |
| LimitPulse（激进） | LightGBM 与逻辑回归构建离散时间风险率模型，研究未来 1–3 日首次触及涨停；逐日 sigmoid 校准，再用 `1 − ∏(1 − hᵢ)` 合成累计事件概率。触板不等于可以买入或盈利。 |
| MASTER（可选挑战者） | 参考 Market-Guided Stock Transformer，组合市场引导特征门控、时间注意力与股票间注意力；使用本地训练权重，独立通过门槛后才参与。 |
| 模型评估 | 按时间滚动训练，隔离标签重叠，分离校准与检验；以 RankIC、NDCG、扣费标签收益和时间块 bootstrap 等检查质量，保留合格冠军模型。 |
| 账户回测 | 按事前信号时间、交易日历、成交限制、滑点和费税进行事件驱动模拟，支持延迟一天的对照；与模型的标签研究分开。 |

SafeBound、BalancedRank、LimitPulse 是本项目的模块名称。展示的排序分位不是胜率；历史检验、论文结果和观察涨跌均不代表实盘收益。模型需在用户选定的股票池上训练，不附带上游预训练权重。

## Windows

Python 3.11+ 可用 pip 安装研究工具；Windows/macOS 桌面 wheel 由私有 GitHub Actions 构建。仓库保持私有，不发布 PyPI。完整安装命令、平台差异和验收范围见 [pip 安装说明](docs/precision-and-packaging.md#私有-github-与-pip)。

开发需要 Python、Node.js 20+、Go（版本见 `desktop/go.mod`）和 Wails 2.11。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r daily-engine/requirements.txt
go install github.com/wailsapp/wails/v2/cmd/wails@v2.11.0
cd desktop
wails dev
```

构建离线安装包（另需 NSIS）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-stock-king-v2.ps1
```

## macOS（Apple Silicon）

支持 Apple Silicon（M 系列）和 macOS 15+，不提供 Intel Mac 构建。需要原生 ARM64 的 Python 3.12、Node.js 20+、Go、Xcode Command Line Tools 和 Homebrew 的 `libomp`。

```bash
xcode-select --install
brew install python@3.12 node go libomp
go install github.com/wailsapp/wails/v2/cmd/wails@v2.11.0
export PATH="$PATH:$(go env GOPATH)/bin"
bash scripts/build-macos.sh
```

产物为 `artifacts/macos/Stock-King-macos-arm64.zip`，解压后把 `Stock King.app` 放入“应用程序”。这是本地临时签名的构建，尚无 Apple 公证；首次打开可能需要在“系统设置 → 隐私与安全性”中允许。GitHub Actions 的 macOS 工作流也会构建同一安装包。

安装独立后台任务（关闭软件窗口后仍可运行）：

```bash
python3 scripts/install-macos-schedule.py
# 停用：python3 scripts/install-macos-schedule.py --remove
```

需保持电脑开机、唤醒并登录。错过的盘中时点不会补造推荐；Mac 使用哪个时区都按北京时间调度。原生 wheel 为 `macosx_15_0_arm64`，包含 LightGBM 和 MASTER 所需 PyTorch。

## 致谢与引用

感谢以下作者、团队及各项目的全部贡献者。Stock King 在这些工作的基础上集成和改造，保留了相应版权与许可声明。

| 作者 / 团队 | 项目与贡献 |
| --- | --- |
| [ArvinLovegood](https://github.com/ArvinLovegood) 及贡献者 | [go-stock](https://github.com/ArvinLovegood/go-stock)：Go / Wails / Vue 桌面基础（GPL-3.0）。 |
| [ZhuLinsen](https://github.com/ZhuLinsen) 及贡献者 | [Daily Stock Analysis](https://github.com/ZhuLinsen/daily_stock_analysis)：研究引擎（MIT）；[AlphaSift](https://github.com/ZhuLinsen/alphasift)：筛选引擎与策略材料（Apache-2.0）。 |
| Tong Li、Zhaoyang Liu、Yanyan Shen、Xue Wang、Haokun Chen、Sen Huang / SJTU-DMTai | [MASTER](https://github.com/SJTU-DMTai/MASTER)：《MASTER: Market-Guided Stock Transformer for Stock Price Forecasting》，AAAI 2024；市场引导注意力架构参考（MIT）。 |
| Microsoft Qlib 团队及贡献者 | [Qlib](https://github.com/microsoft/qlib)：Alpha158 风格特征、DoubleEnsemble 思路及量化研究方法参考；这里是本地实现，不宣称完整复现。 |
| LightGBM、PyTorch、scikit-learn 的作者与维护者 | [LightGBM](https://github.com/lightgbm-org/LightGBM)、[PyTorch](https://github.com/pytorch/pytorch)、[scikit-learn](https://github.com/scikit-learn/scikit-learn)：训练、排序、深度学习与统计建模基础。 |
| AKShare、Wails、Vue、Naive UI 及相关依赖的维护者 | [AKShare](https://github.com/akfamily/akshare) 等数据接口，以及桌面与界面技术生态。 |

完整许可说明见 [第三方声明](THIRD_PARTY_NOTICES.zh-CN.md) 和 [licenses/](licenses/)。

## 开发与许可

桌面端位于 `desktop/`，研究引擎位于 `daily-engine/`。修改后运行相关 Python 测试、前端 `npm test` 和桌面 `go test .`。

方法说明见 [研究流程](docs/RESEARCH_WORKFLOW.md)、[可执行回测](docs/EXECUTABLE_BACKTEST.md)。工程边界见 [INTEGRATION.md](INTEGRATION.md)。

项目许可见 [LICENSE](LICENSE)，上游与第三方许可见 [THIRD_PARTY_NOTICES.zh-CN.md](THIRD_PARTY_NOTICES.zh-CN.md)。仅供研究，不构成投资建议或收益承诺。
