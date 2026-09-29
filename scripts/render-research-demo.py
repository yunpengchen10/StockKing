"""Render a small, reproducible GIF from the public research export.

This is a data walkthrough, not a desktop screen recording or a trading signal.
No application databases, portfolio details, or credentials are read.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from PIL import Image


def render(sample_path: Path, destination: Path) -> None:
    sample = json.loads(sample_path.read_text(encoding="utf-8-sig"))
    # Export schema is documented alongside the data; accept either list key.
    stocks = sample.get("instruments") or sample.get("stocks") or sample.get("securities") or []
    if not isinstance(stocks, list) or not stocks:
        raise ValueError("The export must contain a nonempty stocks list")
    background, panel, text, muted = "#0b1220", "#132036", "#edf4ff", "#9aaec8"
    accent, green = "#75b9ff", "#56d9b0"
    frames: list[Image.Image] = []
    durations: list[int] = []
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 11})

    for step, stock in enumerate(stocks):
        bars = stock.get("dailyBars") or stock.get("daily", {}).get("bars") or []
        bars = [bar for bar in bars if bar.get("close") is not None][-60:]
        if len(bars) < 21:
            raise ValueError(f"Insufficient daily bars for {stock.get('code')}")
        closes = np.asarray([float(bar["close"]) for bar in bars])
        if not np.all(np.isfinite(closes)) or np.any(closes <= 0):
            raise ValueError("Invalid recorded closing prices")
        dates = [str(bar.get("date") or bar.get("end") or bar.get("day"))[:10] for bar in bars]
        ma20 = np.convolve(closes, np.ones(20) / 20, mode="valid")
        name = stock.get("englishName") or {"600000": "SPD Bank", "000001": "Ping An Bank", "600519": "Kweichow Moutai"}.get(stock.get("code"), stock.get("code"))
        source = stock.get("dailySource") or stock.get("daily", {}).get("source") or stock.get("source") or "See source JSON"
        adjustment = stock.get("dailyAdjustment") or stock.get("daily", {}).get("priceAdjustment") or stock.get("daily", {}).get("adjustment") or stock.get("daily", {}).get("priceBasis") or sample.get("priceBasis") or "See source JSON"
        if adjustment == "none":
            adjustment = "unadjusted"

        for view in range(3):
            fig = plt.figure(figsize=(11, 6.2), dpi=95, facecolor=background)
            fig.text(.055, .935, "STOCK KING", color=text, weight="bold", size=21)
            fig.text(.055, .886, "REAL-MARKET RESEARCH REPLAY", color=accent, size=10, weight="bold")
            fig.text(.945, .935, f"0{step+1} / 0{len(stocks)}", color=muted, ha="right", size=12)
            fig.text(.055, .83, f"{stock['code']}  /  {name}", color=text, weight="bold", size=17)
            fig.text(.055, .786, f"Recorded daily closes: {dates[0]} to {dates[-1]}  |  CNY", color=muted, size=10)
            ax = fig.add_axes([.075, .32, .60, .40], facecolor=panel)
            end = len(closes) if view else max(21, len(closes) // 2)
            ax.plot(np.arange(end), closes[:end], color=accent, lw=2.3, label="Close")
            if end >= 20:
                ax.plot(np.arange(19, end), ma20[:end-19], color=green, lw=1.4, label="MA20")
            ax.set_xlim(-1, len(closes))
            spread = max(np.ptp(closes), closes[-1] * .025)
            ax.set_ylim(closes.min() - spread * .15, closes.max() + spread * .15)
            ticks = np.linspace(0, len(closes) - 1, 4, dtype=int)
            ax.set_xticks(ticks, [dates[i][5:] for i in ticks], color=muted, size=9)
            ax.tick_params(axis="y", colors=muted, labelsize=9)
            ax.grid(axis="y", color="#293951", alpha=.65, lw=.65)
            for spine in ax.spines.values():
                spine.set_visible(False)
            ax.legend(loc="upper left", frameon=False, labelcolor=text, fontsize=9)
            fig.text(.73, .67, ["1  Inspect the path", "2  Check the source", "3  Keep uncertainty"][view], color=green, weight="bold", size=12)
            value = closes[end-1]
            fig.text(.73, .598, f"{value:.2f}", color=text, size=25, weight="bold")
            fig.text(.73, .551, f"Close on {dates[end-1]}", color=muted, size=10)
            fig.text(.73, .475, "Source", color=muted, size=9)
            fig.text(.73, .433, str(source)[:27], color=text, size=10)
            fig.text(.73, .384, f"Price basis: {adjustment}", color=muted, size=9)
            lower = [
                "Replay the recorded price path; dates stay visible.",
                "Use the accompanying JSON to inspect source, units and missing data.",
                "Uncalibrated probability and MFE remain unavailable. No trade is implied.",
            ][view]
            fig.text(.055, .211, lower, color=text, size=11)
            fig.text(.055, .153, "Public-data walkthrough, not a desktop screen recording.", color=muted, size=10)
            fig.text(.055, .104, "Recorded observations only. Not a historical recommendation or a return forecast.", color=muted, size=9)
            fig.canvas.draw()
            frames.append(Image.fromarray(np.asarray(fig.canvas.buffer_rgba())[:, :, :3].copy()).convert("P", palette=Image.Palette.ADAPTIVE, colors=128))
            durations.append(1000 if view == 0 else 2300)
            plt.close(fig)
    destination.parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(destination, save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, optimize=True, disposal=2)
    frames[-1].convert("RGB").save(destination.with_suffix(".png"))
    print(f"Rendered {len(frames)} frames: {destination} ({destination.stat().st_size} bytes)")


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", type=Path, default=root / "docs/research/stockking-v11-market-sample.json")
    parser.add_argument("--output", type=Path, default=root / "docs/media/stockking-v11-real-market.gif")
    args = parser.parse_args()
    render(args.sample, args.output)
