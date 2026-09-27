# Stock King

<img src="branding/stock-king-logo.png" alt="Stock King" width="140" />

简体中文 | [English](README.en.md)

面向股票量化与 A 股量化研究的本地桌面软件：行情、K 线、自选、定时精选、账户回测、复盘和手动 AI 研究。支持 Windows x64 与 Apple Silicon（M 系列，macOS 15+）。

- 腾讯、新浪免费行情互为备用，保留真实报价时间；过期或缺失数据不会冒充可执行推荐。
- 工作日 09:20、10:30、14:55 筛选，15:30 复盘，周五 15:45 学习，均按北京时间。
- 自动任务使用本地模型；AI 复审由你手动触发，不增加自动 AI 调用费用。
- 数据、模型和设置保存在本机。软件不自动下单。
- 核心流程无需付费 API、云服务器或 GitHub Actions；构建和校验在自己的电脑完成。AI 可使用本地 Ollama，也可手动导入报告。

需要让 AI 工具复用同一套行情，可使用 [只读 MCP 行情服务](quote-service/README.md)。支持 Windows、macOS 的 Python 运行方式；ChatGPT 云端仍需完成远程连接授权和实测。

## 快速开始

1. 获取维护者提供的原生 wheel，或按 [本地构建与分发](docs/LOCAL_BUILD.md) 在自己的 Windows / Apple Silicon 电脑打包。无需 Actions、付费构建平台或云服务器；当前仓库仍是私有内测，源码访问需要授权。
2. 建议使用 64 位 Python 3.12。安装已有的原生 wheel，然后启动：

   ```text
   python -m pip install "下载目录/stock_king-2.6.0-py3-none-win_amd64.whl"
   stock-king
   ```

   Apple Silicon 使用 `stock_king-2.6.0-py3-none-macosx_15_0_arm64.whl`；Python 也须为 ARM64。Windows 需要 WebView2 Runtime。Mac 也可直接使用构建产物中的 `.app` 压缩包。
3. 打开“自选”添加研究股票，在“精选 → 当日机会”点“刷新全部”，先检查数据时间、证据缺口与入场条件。
4. 需要 AI 时再配置模型；看行情、运行本地规则不要求 AI API Key。进入“策略”前先选择训练股票池或准备回测数据。

当前未发布到 PyPI，也尚无公开安装包下载页。`pip install stock-king` 不是本仓库当前的安装方式。已有仓库权限的用户可先安装研究 CLI：

```text
python -m pip install "stock-king[data] @ git+https://github.com/yunpengchen10/StockKing-clean.git@main"
stock-king doctor
```

源码安装提供研究 CLI，完整桌面请使用平台原生 wheel。项目显示名为 **Stock King**，GitHub 仓库名为 **StockKing**；兼容现有安装的命令仍为 `stock-king`。详情见 [本地构建与分发](docs/LOCAL_BUILD.md)。

## 页面与使用流程

界面当前以中文为主；英文 README 中保留中文菜单名，便于对照操作。

| 页面入口 | 用途与操作 |
| --- | --- |
| 市场 | 查看市场概览、交易时段与研究线索，再决定今天关注哪些股票。 |
| 自选 | 搜索股票、加入或整理分组；从自选进入个股图表与 AI 研究。自选记录不代表真实券商持仓。 |
| 图表 | 查看 K 线、技术指标与价量结构，核对当前价格、趋势和交易计划。 |
| 精选 → 当日机会 | 刷新扫描，在稳健/均衡/激进之间切换；阅读“为何入选”、指标证据、触发/失效/不追条件；历史和推荐记录可回看当时快照。 |
| 精选 → 策略分组 | 查看原有策略分组及模型状态；与当日机会的三档入场过滤分开。 |
| AI 研究 | 选择股票与研究模板，提取证据后手动调用已配置模型；也可导出研究包、手动导入外部 AI 报告，比较共识、分歧与证据时间。 |
| 策略 | “账户回测”导入行情和事前信号、设置费用与成交约束、查看净值及成交记录；模型研究在所选股票池内训练并检查资格状态。 |
| 多图 | 在同一工作区对比多只股票的图表。 |
| 工具 | 进入 AI 平台配置、研报与深度研究、基金研究、研究助手、计划任务、数据与工具等入口。 |
| 设置 | 管理偏好、行情刷新、通知及 AI 诊股等设置。配置修改后保存。 |

建议顺序：**市场 → 自选 → 精选 → 图表核验 → 可选 AI 复审 → 历史复盘**。空候选、待核验或过期都是有效状态；不要把未知数据当成已经通过，也不要把观察涨跌当成真实成交收益。

## AI 配置说明

**免费入门：** 不配置 AI 也能使用核心功能。需要本地 AI 时，在下面配置流程中使用 Ollama：Base URL 为 `http://localhost:11434/v1`，令牌填本地占位值 `ollama`，Model ID 填已安装模型的准确标签。也可在“AI 研究”导出研究包、手动导入已有报告。本地运行需要相应内存和算力；外部报告所用服务由用户自行选择。

1. 打开“工具 → AI 平台配置”，点击“添加AI配置”。也可从“设置 → AI设置”启用“AI诊股”后进入“前往管理”；该旧入口的开关不代替模型配置。
2. 填写**配置名称、接口地址（Base URL）、令牌（API Key）、模型名称（Model ID）**。接口须支持应用使用的 OpenAI 兼容 Chat Completions 格式；模型名称以服务商实际可用列表为准。
3. 点击“测试并刷新模型列表”。它检查模型列表接口，不等于已经验证模型生成回答或推理参数。
4. 抽屉中点击“确定”，再点击列表页的 **“保存配置”**，看到保存成功后再离开。
5. 返回“AI 研究”，或“精选 → 当日机会”，选择平台和模型，手动发起研究或“AI 复审”。模型调用可能产生服务商费用。

| 字段 | 填写说明 |
| --- | --- |
| Base URL | 填服务商的 API 基址，保留其要求的 `/v1` 等前缀；不要填写聊天网页或追加 `/chat/completions`。 |
| API Key | 使用对应 API 平台的密钥；网页登录或聊天订阅不等于此处已配置 API 访问。仅在本机配置页填写。 |
| Model ID | 从列表选择或手动填写准确 ID，不能用显示昵称代替。 |
| Temperature / MaxTokens | 按模型能力设置采样参数与最大输出量；模型不支持某参数时，以服务商说明为准。 |
| Timeout / 深度思考 | Timeout 单位为秒；推理模型可需要更长时间。仅在模型和接口兼容时启用深度思考。 |
| HTTP 代理 | 按实际网络需要配置；模型列表测试使用通用 HTTP 客户端，不能据此确认单个配置的代理已生效。 |

完整示例、Ollama 本地配置、手动导入报告、密钥与常见错误处理见 [AI 配置说明书](docs/AI_CONFIGURATION.md)。保存的配置位于本机，但手动使用远程 AI 时，所选证据、提示词及请求内容会发送给该服务商。日常定时扫描不会自动调用 AI；自行启用的机器人或其他 AI 任务需单独管理。

## 常见问题与本地验证

| 现象 | 处理方式 |
| --- | --- |
| 旧提交仍显示失败检查 | 历史任务曾被 GitHub 账单限制阻止启动。项目现已停用 Actions，不需要开通付费额度；旧结果不会因此变为通过。 |
| 没有安装包或包已过期 | 使用维护者提供的本地构建包，或按本地构建说明重新打包；不依赖 Actions 临时产物。 |
| 引擎未就绪、行情缺失或精选为空 | 先确认网络与引擎状态，再看报价时间和缺口；完整证据不够时允许没有候选。 |
| 连接测试成功，AI 仍然报错 | 模型列表与生成回答是不同接口；检查模型权限、余额、模型 ID、推理参数及实际请求错误。 |
| 安装提示 wheel 不支持当前平台 | 核对 Windows x64 / macOS ARM64、Python 架构与 macOS 15+ 要求。Intel Mac 不在支持范围。 |

本仓库不使用 GitHub Actions，也不要求购买 GitHub Pro 或付费构建服务。本地构建脚本执行对应测试和安装校验；Windows 与 Apple Silicon 应分别在对应机器验收。操作见 [本地构建与分发](docs/LOCAL_BUILD.md)，历史失败说明见 [构建排查](docs/CI_TROUBLESHOOTING.md)。

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

Python 3.11+ 可用 pip 安装研究工具，建议 3.12。桌面 wheel 在本地电脑构建，仓库目前保持私有，尚未发布 PyPI。完整安装命令和平台差异见 [本地构建与分发](docs/LOCAL_BUILD.md)。

开发需要 Python、Node.js 20+、Go（版本见 `desktop/go.mod`）和 Wails 2.11。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r daily-engine/requirements.txt
go install github.com/wailsapp/wails/v2/cmd/wails@v2.11.0
cd desktop
wails dev
```

构建原生桌面 wheel（无需 NSIS、Actions 或付费证书）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/build-windows-wheel.ps1
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

产物为 `artifacts/macos/Stock-King-macos-arm64.zip` 及 `dist/desktop/` 中的 wheel，解压后把 `Stock King.app` 放入“应用程序”。这是本地临时签名的构建，不要求购买开发者证书，尚无 Apple 公证；首次打开可能需要在“系统设置 → 隐私与安全性”中允许。

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
