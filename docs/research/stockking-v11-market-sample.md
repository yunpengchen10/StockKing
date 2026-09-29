# Stock King V1.1：真实行情研究样本

[English](stockking-v11-market-sample.en.md) · [样本 JSON](stockking-v11-market-sample.json)

这是 **2026-09-29 收盘后** 对浦发银行（600000）、平安银行（000001）和贵州茅台（600519）的公开行情观察。数据于北京时间 17:20:20 导出，用来展示行情来源、缺口与基础统计的表达方式。它不是当时运行的 Stock King 历史扫描，也不是这三只股票的推荐记录；不能用于计算 V1.1 策略绩效。

## 数据和口径

- **报价：**软件的 `public_market_quotes` 通道核验腾讯、新浪公开报价。本次三个记录采用腾讯报价，源更新时间为 2026-09-29 16:14:57～16:15:00，均已过实时核验窗口，只能作为收盘后参考。`sourceTime` 是供应商更新时间，不能当作成交时间。
- **日线：**软件的 `screening.daily.fetch_daily_history(source='sina')` 读取新浪无复权日线，每股 60 根，日期为 2026-07-07 至 2026-09-29。价格收益只比较收盘价，未计入分红、交易费用或滑点。
- **分钟：**软件的 `yao_scout.minute_history.fetch_sina_bars` 读取新浪无复权 1 分钟 K 线；JSON 保留 2026-09-29 最后 12 根及各日覆盖统计。当天三只股票均只有 **238/240** 根预期分钟，不能把分钟额求和伪称完整全天成交额。
- **成交额：**新浪日线接口未给出日成交额。60 根日线中，每股只有 2026-09-29 的 `amount_cny` 使用同日腾讯收盘后累计成交额，`amountSource` 已逐项标记；其余 59 根为 `null`，没有插值。

## 简单观察

| 股票 | 9月29日收盘价 | 近5交易日价格变化 | MA5 / MA20 | 20日收益年化波动率 |
| --- | ---: | ---: | ---: | ---: |
| 浦发银行 600000 | ¥9.18 | +1.887% | ¥9.07 / ¥9.19 | 19.45% |
| 平安银行 000001 | ¥11.35 | −3.240% | ¥11.45 / ¥11.70 | 16.15% |
| 贵州茅台 600519 | ¥1,235.58 | −1.356% | ¥1,244.30 / ¥1,275.46 | 13.38% |

近 5 交易日价格变化是 `最近收盘价 / 5 个交易日前收盘价 − 1`。MA5、MA20 是最近 5、20 根收盘价算术平均。波动率取最近 20 个日对数收益的样本标准差并乘以 `√252`。这些数值描述过去的价格路径，不能表示未来收益、实际可成交收益或主升概率。

## 复现

在仓库根目录、已安装项目 Python 依赖的环境中运行：

```powershell
python daily-engine/scripts/export_public_research_sample.py --as-of 2026-09-29 --output docs/research/stockking-v11-market-sample.json
```

脚本只访问公开行情接口，不读取用户数据库、持仓、密钥或自选。接口会更新，重新导出的源时间与结果可能不同。阅读时应逐项检查 JSON 中的 `sources`、`quote.sourceTime`、`daily.bars[].amountSource`、`minute.coverageByDate` 和 `gaps`。

## 可选动画演示

[真实行情研究回放 GIF](../media/stockking-v11-real-market.gif) 由上述 JSON 生成，**不是 Stock King 桌面软件录屏**，也不表示这些股票曾被系统推荐。动画所需的可选 Python 依赖为 `matplotlib`、`numpy` 和 `Pillow`。在仓库根目录运行：

```powershell
python -m pip install matplotlib numpy Pillow
python scripts/render-research-demo.py --sample docs/research/stockking-v11-market-sample.json --output docs/media/stockking-v11-real-market.gif
```

V1.1 的主升概率、预期 MFE/MAE 均保留 `null`：这里没有三只股票当时的事前信号、第一正常可成交价格或经过验证的模型。**本页不构成买卖建议。**
