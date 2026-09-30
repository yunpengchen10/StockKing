# Stock King · 本地股票研究工作台

<img src="branding/stock-king-logo.png" alt="Stock King" width="140" />

简体中文 | [English](README.en.md)

面向股票量化与 A 股量化研究的本地桌面软件：行情、K 线、自选、定时精选、账户回测、复盘和手动 AI 研究。支持 Windows x64 与 Apple Silicon（M 系列，macOS 15+）。

## 看看软件怎么用

<p align="center">
  <img src="https://raw.githubusercontent.com/yunpengchen10/StockKing/main/docs/media/stockking-workspace-tour.gif" alt="Stock King 操作演示：自选、K 线图表、精选与复盘" width="1000" />
</p>

**当前 Vue 前端的实际界面，使用隔离的演示数据。** 动图展示软件操作，不代表实时行情、真实推荐或实际收益。[静态预览](docs/media/stockking-workspace-tour.png) · [演示数据与复现方法](docs/WORKSPACE_TOUR.md)

[快速开始](#快速开始) · [Windows 安装](docs/WINDOWS_INSTALL.md) · [本地构建](docs/LOCAL_BUILD.md) · [AI 配置](docs/AI_CONFIGURATION.md) · [MCP 行情服务](quote-service/README.md)

## 主要功能

- 图表与精选共用软件的通达信、东方财富、新浪、腾讯行情服务及缓存，保留来源时间与获取时间；关闭图表后仍可运行扫描。
- 09:20 盘前观察；09:40、09:55、10:30、14:55 独立扫描；15:30 归档与到期复盘；周五 15:45 学习，均按北京时间并检查交易日。
- 精选采用 StockKing V1.1 本地算法，深研队列按主板池的 10% 向上取整、至少 300 只且不超过实际股票数安排，每轮最多入选 5 只、不凑数；显示实际覆盖和缺口，推荐、复盘和学习全程不调用大模型。
- 数据、模型和设置保存在本机。软件不自动下单。
- 核心流程无需付费 API、云服务器或 GitHub Actions；构建和校验在自己的电脑完成。AI 可使用本地 Ollama，也可手动导入报告。

需要让 AI 工具复用同一套行情，可使用 [只读 MCP 行情服务](quote-service/README.md)。支持 Windows、macOS 的 Python 运行方式；ChatGPT 云端仍需完成远程连接授权和实测。

## 本次代码更新

- **刷新看得见进度：** 本地精选使用后台任务，展示扫描阶段；切换页面或行情请求失败时保留上次有效结果。
- **推荐记录更完整：** 修复大响应截断，单独读取最新报价，同时保留推荐时的原始价格与时间。
- **研究覆盖更清楚：** 扩大深研队列，记录各阶段耗时、请求预算与数据缺口；必要证据不足时保留说明。
- **训练与精选口径一致：** 记录算法契约，让准入、排序与验证使用同一套规则；独立日线模型单独展示。

<details>
<summary>另看：带日期与来源的真实行情研究回放</summary>

![StockKing V1.1 真实行情研究回放](docs/media/stockking-v11-real-market.gif)

这是已记录行情的研究回放，非桌面录屏。演示股票不代表当时扫描入选、用户真实成交或未来收益。查看[示例口径与复现说明](docs/research/stockking-v11-market-sample.md)和[原始行情样本](docs/research/stockking-v11-market-sample.json)。

</details>

## 快速开始

Windows v2.6.2 提供 `Stock-King-Setup-x64-v2.6.2.exe` 一键安装包，包含本地引擎和离线 WebView2，无需安装 Python、Go 或 Node.js；修复了推荐记录响应超过 16 MiB 时被截断、无法显示的问题，并包含 v2.6.1 的窗口启动修复。参见[安装与修复说明](docs/WINDOWS_INSTALL.md)。以下 wheel 为可选安装方式。

1. 已有安装包时，Mac 用户直接解压 `Stock-King-macos-arm64.zip`，将 `Stock King.app` 放入“应用程序”并打开；看到界面左下角“已连接”即表示本地引擎已启动。此方式无需安装 Python、Go 或 Node.js。Windows 也可选择平台原生 wheel。也可从本公开仓库获取源码，按 [本地构建与分发](docs/LOCAL_BUILD.md) 在自己的电脑构建，无需 Actions、付费构建平台或云服务器。
2. 使用原生 wheel 时，安装对应平台的 64 位 Python 3.11+（推荐 3.12），在 wheel 所在目录运行（版本号变化时使用实际文件名）：

   ```text
   python -m pip install ./stock_king-2.6.0-py3-none-win_amd64.whl
   stock-king
   ```

   Apple Silicon 使用 `stock_king-2.6.0-py3-none-macosx_15_0_arm64.whl`；Python 也须为 ARM64。Windows 需要 WebView2 Runtime。Mac 也可直接使用构建产物中的 `.app` 压缩包。
3. 打开“自选”添加研究股票，在“精选 → 本地精选”点“刷新扫描”，检查行情时间、实际覆盖天数、因子和触发/失效条件；点击“图表”核对同一行情链路。
4. 在“推荐记录”“延后复盘”“学习状态”查看保存的信号、T+1/3/5结果与样本积累。自动推荐和自动学习分别设置；V1.1从已记录的主板信号与对照样本学习，无需另选训练股票池。
5. 需要手动 AI 研究时再配置模型。独立“策略”页的模型研究仍需选择训练股票池，账户回测需准备行情及事前信号。

当前未发布到 PyPI，也尚无公开安装包下载页。`pip install stock-king` 不是本仓库当前的安装方式。可从公开仓库安装研究 CLI：

```text
python -m pip install "stock-king[data] @ git+https://github.com/yunpengchen10/StockKing.git@main"
stock-king doctor
```

源码安装提供研究 CLI，完整桌面请使用 Windows 一键安装包、平台原生 wheel 或 macOS `.app` 安装包。项目显示名为 **Stock King**，GitHub 仓库名为 **StockKing**；兼容现有安装的命令仍为 `stock-king`。详情见 [本地构建与分发](docs/LOCAL_BUILD.md)。

## 页面与使用流程

界面当前以中文为主；英文 README 中保留中文菜单名，便于对照操作。

| 页面入口 | 用途与操作 |
| --- | --- |
| 市场 | 查看市场概览、交易时段与研究线索，再决定今天关注哪些股票。 |
| 自选 | 搜索股票、加入或整理分组；从自选进入个股图表与 AI 研究。自选记录不代表真实券商持仓。 |
| 图表 | 查看 K 线、技术指标与价量结构，核对当前价格、趋势和交易计划。 |
| 精选 → 本地精选 | 运行V1.1扫描，查看Early/MainRise评分、风险、历史覆盖和可观察价格结构；阅读入选理由与触发/失效条件。 |
| 精选 → 推荐记录 / 延后复盘 / 学习状态 | 按日期、股票、算法版本查看各轮信号与对照；同股同日汇总显示。区分观察价格变化和模拟成交，查看成熟样本、晋升及影子验证状态。 |
| 精选 → 策略分组 | 查看原有策略分组及模型状态；使用各自的目标和训练口径。 |
| AI 研究 | 选择股票与研究模板，提取证据后手动调用已配置模型；也可导出研究包、手动导入外部 AI 报告，比较共识、分歧与证据时间。 |
| 策略 | “账户回测”导入行情和事前信号、设置费用与成交约束、查看净值及成交记录；模型研究在所选股票池内训练并检查资格状态。 |
| 多图 | 在同一工作区对比多只股票的图表。 |
| 工具 | 进入 AI 平台配置、研报与深度研究、基金研究、研究助手、计划任务、数据与工具等入口。 |
| 设置 | 管理偏好、行情刷新、通知及 AI 诊股等设置。配置修改后保存。 |

建议顺序：**市场 → 自选 → 本地精选 → 图表核验 → 推荐记录 → 延后复盘**。需要额外研究时单独进入“AI 研究”。空候选、待核验或过期都是有效状态；不要把未知数据当成已经通过，也不要把观察涨跌当成真实成交收益。

## AI 配置说明

**免费入门：** 不配置 AI 也能使用核心功能。需要本地 AI 时，在下面配置流程中使用 Ollama：Base URL 为 `http://localhost:11434/v1`，令牌填本地占位值 `ollama`，Model ID 填已安装模型的准确标签。也可在“AI 研究”导出研究包、手动导入已有报告。本地运行需要相应内存和算力；外部报告所用服务由用户自行选择。

1. 打开“工具 → AI 平台配置”，点击“添加AI配置”。也可从“设置 → AI设置”启用“AI诊股”后进入“前往管理”；该旧入口的开关不代替模型配置。
2. 填写**配置名称、接口地址（Base URL）、令牌（API Key）、模型名称（Model ID）**。接口须支持应用使用的 OpenAI 兼容 Chat Completions 格式；模型名称以服务商实际可用列表为准。
3. 点击“测试并刷新模型列表”。它检查模型列表接口，不等于已经验证模型生成回答或推理参数。
4. 抽屉中点击“确定”，再点击列表页的 **“保存配置”**，看到保存成功后再离开。
5. 返回独立“AI 研究”页，选择平台、模型和股票，再手动发起研究。模型调用可能产生服务商费用；精选页已移除AI推荐、复审和模型选择。

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
| 引擎未就绪、行情缺失或精选为空 | 先确认网络与引擎状态，再检查报价新鲜度、交易状态和实际覆盖；必要数据不合格时允许空候选，可选因子缺失则按可用因子计算并标明缺口。 |
| 连接测试成功，AI 仍然报错 | 模型列表与生成回答是不同接口；检查模型权限、余额、模型 ID、推理参数及实际请求错误。 |
| 安装提示 wheel 不支持当前平台 | 核对 Windows x64 / macOS ARM64、Python 架构与 macOS 15+ 要求。Intel Mac 不在支持范围。 |

本仓库不使用 GitHub Actions，也不要求购买 GitHub Pro 或付费构建服务。本地构建脚本执行对应测试和安装校验；Windows 与 Apple Silicon 应分别在对应机器验收。操作见 [本地构建与分发](docs/LOCAL_BUILD.md)，历史失败说明见 [构建排查](docs/CI_TROUBLESHOOTING.md)。

## 推荐算法

精选采用 **StockKing V1.1**：每轮重新读取沪深主板非 ST 快照，按 `min(实际股票数, max(300, ceil(实际股票数 × 10%)))` 安排深研队列，最多入选 5 只。队列规模不等于完成核验数量：软件记录实际覆盖、超时和缺口。每轮重新选队列，允许新启动股票进入；未深度核验不等于已经排除。

冷启动版本为 `stockking-v1.1-rules`，保留 Early / MainRise 两个通道及原始人工权重，可用因子重新分配权重：

```text
FinalScore = clip(max(EarlyScore, MainRiseScore) - 0.2 × DistributionRisk, 0, 100)
```

`0.2` 是未验证的初始风险系数；评分不代表概率或收益承诺，不再使用概率连乘及 65/75/85 固定等级。尚未训练校准的 MFE、MAE 预测和主升/阶段概率显示“待校准”，阶段说明使用可观察的价格结构。详见 [V1.1 数据、评分与学习口径](docs/stockking-v11-integration.md)。

| 环节 | 实际实现 |
| --- | --- |
| 共用行情 | 图表与 Python 精选通过带鉴权的本机 Go 接口共用行情和缓存，无需打开图表。统一证券代码、分钟结束时间、成交量“股”和成交额“元”，保留来源时间、获取时间及复权口径。未知成交额保留为空，不写成零。 |
| 因子计算 | 使用原始行情计算动量、同刻量能、换手、VWAP、突破、板块及风险特征。09:40 使用短窗口，15 分钟完整后启用对应因子；MAD 加零值保护，不使用未来或尚未完成的分钟。 |
| 历史覆盖与缺口 | 同刻样本达到 20 日使用完整基准；5–19 日按实际样本计算并降低置信度；不足 5 日的历史因子为空。缺少资金、催化或其他可选因子时按可用权重计算，并展示实际覆盖天数。 |
| 必要检查 | 当前报价、证券身份和必要交易状态必须有效；陈旧报价标记数据不足，停牌或无卖盘封板等不可成交对象不作为可执行推荐。09:20 仅列盘前观察，不把普通快照当作竞价字段。 |
| 先保存再展示 | 每条信号保存唯一编号、算法版本、扫描及行情时间、因子、排序、原因、风险和触发/失效条件；保留各轮入选及深度核验未入选的对照样本。同股同日汇总，重复任务幂等，手动刷新不重复计入训练。 |
| 延后复盘 | 按交易日历在 T+1、T+3、T+5 收盘后补齐观察涨跌、MFE 和正数口径的 MAE。未到期为“待复盘”，缺数据为“待补齐”。旧记录保留原口径，不补写当时未知指标，也不混入新版训练。 |
| 模拟成交 | 观察价格变化与模拟收益分开保存。模拟按信号后下一完整分钟入场、期限收盘退出，并应用账户回测的成交约束与费用；无法成交记为未成交。模拟结果不代表用户实际交易。 |

### 本地后台任务

自动推荐与自动学习有独立开关。Windows 任务与 macOS 调度均按北京时间运行，检查交易日并跨进程去重；错过时点只记录遗漏，不补造历史推荐。

| 时间（北京时间） | 工作 |
| --- | --- |
| 09:20 | 盘前观察池 |
| 09:40 | 短窗口独立扫描 |
| 09:55、10:30、14:55 | 重新读取主板快照并独立扫描 |
| 15:30 | 当日归档及已到期的 T+1/3/5 复盘 |
| 周五 15:45 | 训练、验证和模型评估 |

### 自动学习与晋升

V1.1 只使用已记录并确认的主板信号与对照样本，采用独立模型和标签版本；逻辑回归作为基线，LightGBM 作为候选。默认至少积累 **120 个独立成熟交易日、1000 条有效样本**后，才开始晋升检验。训练、校准和检验按时间划分，边界至少隔离 5 个交易日。

只有样本外扣费收益和相对现行规则收益优势的 **95% 置信下界均大于零**，且回撤未恶化，候选才进入 **20 个交易日的前瞻影子验证**；通过后自动启用。样本不足或未通过时继续使用当前规则或已合格版本。这些门槛是待验证模型的准入条件，不代表目前已经获得有效盈利模型。

学习版按可实现收益与下行风险排序，分数映射为校准集百分位；保留上版模型，推理失败或版本不兼容时自动回退。推荐、复盘、训练和晋升均在本机执行，不调用大模型。

## 量化算法

以下为独立“策略”页与策略分组中的研究模块，使用各自的训练股票池及评估目标；V1.1 精选使用上文的信号记录与晋升流程。

| 模块 | 算法与用途 |
| --- | --- |
| 量价特征 | Alpha158 风格的收益、动量、波动、价量关系和 K 线形态特征；只使用当时及此前的数据。 |
| BalancedRank（常规） | LightGBM LambdaRank、DoubleEnsemble 式难样本重加权及 Ridge 基线，研究 5 / 20 日横截面排序；结合暴露中性化与样本外非负组合权重。 |
| SafeBound（保守） | LightGBM 分位数回归估计 20 / 60 日收益下界，配合样本外分位数校正、波动及回撤预测；使用 LightGBM、滚动波动率 / HAR 风格基线作比较。 |
| LimitPulse（激进） | LightGBM 与逻辑回归构建离散时间风险率模型，研究未来 1–3 日首次触及涨停；逐日 sigmoid 校准，再用 `1 − ∏(1 − hᵢ)` 合成累计事件概率。触板不等于可以买入或盈利。 |
| MASTER（可选挑战者） | 参考 Market-Guided Stock Transformer，组合市场引导特征门控、时间注意力与股票间注意力；使用本地训练权重，独立通过门槛后才参与。 |
| 模型评估 | 按时间滚动训练，隔离标签重叠，分离校准与检验；以 RankIC、NDCG、扣费标签收益和时间块 bootstrap 等检查质量，保留合格冠军模型。 |
| 账户回测 | 按事前信号时间、交易日历、成交限制、滑点和费税进行事件驱动模拟，支持延迟一天的对照；与模型的标签研究分开。 |

SafeBound、BalancedRank、LimitPulse 是本项目的模块名称。展示的排序分位不是胜率；历史检验、论文结果和观察涨跌均不代表实盘收益。上述独立研究模块需在用户选定的股票池上训练，不附带上游预训练权重。

## Windows

Python 3.11+ 可用 pip 从本公开仓库安装研究工具，建议 3.12。桌面 wheel 在本地电脑构建，尚未发布 PyPI。完整安装命令和平台差异见 [本地构建与分发](docs/LOCAL_BUILD.md)。

开发需要 Python、Node.js 20+、Go 1.26（版本见 `desktop/go.mod`）和 Wails 2.11。本项目的 Wails 2.11 绑定生成在 Go 1.27 上曾报错，构建脚本固定使用 Go 1.26.8。

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

支持 Apple Silicon（M 系列）和 macOS 15+，不提供 Intel Mac 构建。构建需要原生 ARM64 的 Python 3.11 或 3.12（推荐 3.12）、Node.js 20+、Go 1.26、Xcode Command Line Tools 和 Homebrew 的 `libomp`。先确认命令行工具已安装：运行 `xcode-select -p`；若报错，运行 `xcode-select --install` 并等待系统安装完成。若没有 `brew` 命令，先按 [Homebrew 官方安装说明](https://brew.sh/)安装。再从 [Python 官方 macOS 下载页](https://www.python.org/downloads/macos/)安装 universal2 版本的 Python；如果已安装可用的 ARM64 Python，可跳过这一步。

```bash
brew install node go@1.26 libomp
export PATH="$(brew --prefix go@1.26)/bin:$PATH"
git clone https://github.com/yunpengchen10/StockKing.git
cd StockKing
bash scripts/build-macos.sh
```

脚本会检查 Python 版本与架构，自动安装 Wails 2.11，并在现有 `.venv` 不兼容时重建。只有 Python 3.11 可用时，可运行 `PYTHON_BIN=python3.11 bash scripts/build-macos.sh`。若终端通过代理上网，请在构建前设置实际可用的 `HTTPS_PROXY` 和 `HTTP_PROXY`；系统设置中的代理不一定会被终端工具使用。首次构建需下载并编译较大的前端、Go 和 Python 依赖，会花较长时间。

产物为 `artifacts/macos/Stock-King-macos-arm64.zip` 及 `dist/desktop/` 中的 wheel。解压 ZIP，把 `Stock King.app` 放入“应用程序”并打开，确认界面显示“已连接”。脚本在非云同步的临时目录签名，避免 `Documents` 等目录的扩展属性导致签名失败。这是本地临时签名的构建，不要求购买开发者证书，尚无 Apple 公证；首次打开可能需要在“系统设置 → 隐私与安全性”中允许。

如需独立后台任务，在源码目录执行以下命令（关闭软件窗口后仍可运行）：

```bash
python3 scripts/install-macos-schedule.py
# 停用：python3 scripts/install-macos-schedule.py --remove
```

后台任务是安装 `.app` 后单独启用的，默认寻找 `/Applications/Stock King.app`。需保持电脑开机、唤醒并登录。工作日 09:10、09:30、09:45、10:20、14:45 开始准备，对应 09:20 观察和 09:40、09:55、10:30、14:55 扫描；15:30 归档复盘，周五 15:45 学习。Mac 使用哪个时区都按北京时间调度，错过的盘中时点不会补造推荐。原生 wheel 为 `macosx_15_0_arm64`，包含 LightGBM 和 MASTER 所需 PyTorch。

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
