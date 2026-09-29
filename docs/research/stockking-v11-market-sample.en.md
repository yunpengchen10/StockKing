# Stock King V1.1: public market research sample

[简体中文](stockking-v11-market-sample.md) · [Sample JSON](stockking-v11-market-sample.json)

This is a **post-close observation for 2026-09-29** of Shanghai Pudong Development Bank (600000), Ping An Bank (000001), and Kweichow Moutai (600519). It was exported at 17:20:20 China time to demonstrate source labels, missing data, and basic statistics. It is neither a historical Stock King scan nor a recommendation of these securities at that time, so it cannot measure V1.1 strategy performance.

## Sources and definitions

- **Quotes:** Stock King's `public_market_quotes` adapter checks public Tencent and Sina quotes. These three records used Tencent, with provider update times between 16:14:57 and 16:15:00 on 2026-09-29. All had passed the live freshness window and are post-close references. Provider `sourceTime` is not an execution time.
- **Daily bars:** `screening.daily.fetch_daily_history(source='sina')` retrieved 60 unadjusted Sina bars per security, spanning 2026-07-07 through 2026-09-29. Price returns exclude dividends, trading costs, and slippage.
- **Minute bars:** `yao_scout.minute_history.fetch_sina_bars` retrieved unadjusted Sina one-minute bars. The JSON retains the final 12 bars of 2026-09-29 and coverage by day. Each security had only **238 of 240** expected bars that day; the observed minute amount is not treated as the complete daily amount.
- **Trading amount:** The Sina daily endpoint did not supply daily amount. Only the 2026-09-29 `amount_cny` for each security uses its same-day Tencent post-close cumulative quote, labelled in `amountSource`. The other 59 daily amount values remain `null` without estimation.

## Simple observations

| Security | Sep 29 close | Five-session price change | MA5 / MA20 | Annualized volatility of 20 daily returns |
| --- | ---: | ---: | ---: | ---: |
| Shanghai Pudong Development Bank 600000 | ¥9.18 | +1.887% | ¥9.07 / ¥9.19 | 19.45% |
| Ping An Bank 000001 | ¥11.35 | −3.240% | ¥11.45 / ¥11.70 | 16.15% |
| Kweichow Moutai 600519 | ¥1,235.58 | −1.356% | ¥1,244.30 / ¥1,275.46 | 13.38% |

The five-session change is `latest close / close five trading sessions earlier − 1`. MA5 and MA20 are arithmetic means of the latest five and 20 closes. Volatility is the sample standard deviation of 20 daily log returns multiplied by `√252`. These describe past prices; they do not imply future returns, executable returns, or a probability of a main rise.

## Reproduce the export

From the repository root, with the project's Python dependencies installed:

```powershell
python daily-engine/scripts/export_public_research_sample.py --as-of 2026-09-29 --output docs/research/stockking-v11-market-sample.json
```

The script calls only public market endpoints. It does not read user databases, positions, keys, or watchlists. Endpoints update, so a new export may have different timestamps or values. Inspect `sources`, `quote.sourceTime`, `daily.bars[].amountSource`, `minute.coverageByDate`, and `gaps` in the JSON.

## Optional animated walkthrough

The [real-market research replay GIF](../media/stockking-v11-real-market.gif) is generated from the JSON above. It is **not a recording of the Stock King desktop app**, and it does not imply that the app recommended these securities. The optional Python dependencies for this animation are `matplotlib`, `numpy`, and `Pillow`. Run from the repository root:

```powershell
python -m pip install matplotlib numpy Pillow
python scripts/render-research-demo.py --sample docs/research/stockking-v11-market-sample.json --output docs/media/stockking-v11-real-market.gif
```

V1.1 main-rise probability and expected MFE/MAE remain `null`: no point-in-time signals, first normally tradable prices, or validated model exist for these three historical examples. **This page is not investment advice.**
