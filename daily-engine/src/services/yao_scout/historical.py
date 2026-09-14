"""Fixed regression cases and the traceable historical research compilation."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from src.formatters import markdown_to_html_document


CASE_STUDIES: list[dict[str, Any]] = [
    {
        "code": "600127",
        "name": "金健米业",
        "thesis": "前期量价脉冲后回撤蓄势，随后连板加速；高换手阶段的可交易性仍在，但拥挤和回撤风险陡升。",
        "timeline": [
            ("2026-08-06", "+9.98%", "7.40%", "约3.86", "首次量价脉冲"),
            ("2026-08-07", "+2.43%", "14.43%", "约6.48", "高换手承接"),
            ("2026-08-14", "-3.93%", "5.14%", "—", "回撤与筹码再分配"),
            ("2026-08-17~20", "+46.51%", "区间62.45%", "—", "连续涨停并进入过热区"),
        ],
        "event": "公司异常波动/风险提示披露基本面未发生重大变化，且半年度预计亏损。",
        "leakage_guard": "8月17日以后披露的异常波动和风险提示只能用于盘后解释，不能进入8月17日盘前特征。",
        "source": "上海证券交易所上市公司公告",
        "url": "https://www.sse.com.cn/disclosure/listedinfo/announcement/",
    },
    {
        "code": "003040",
        "name": "楚天龙",
        "thesis": "题材热度抬升与放量反包形成共振；业务相关性必须单独核验，避免把概念曝光误当成收入弹性。",
        "timeline": [
            ("2026-08-19", "-5.90%", "2.83%", "—", "启动前收缩"),
            ("2026-08-20", "+7.19%", "6.68%", "约1.65", "放量反包"),
            ("2026-08-21~26", "连续涨停", "逐日放大", "—", "题材加速与拥挤"),
        ],
        "event": "异常波动公告提示数字人民币相关业务收入占比不足5%，概念与业绩贡献存在显著落差。",
        "leakage_guard": "异常波动公告及上涨后媒体解读不得回填到8月20日以前的候选证据。",
        "source": "巨潮资讯公司公告",
        "url": "https://www.cninfo.com.cn/new/disclosure",
    },
    {
        "code": "002084",
        "name": "海鸥住工",
        "thesis": "控制权事件是核心催化，但停复牌后连续一字板几乎无法买入；低换手是不可交易，不是弱势。",
        "timeline": [
            ("2026-08-14", "+6.21%", "7.74%", "约2.51", "停牌前异动"),
            ("2026-08-17", "停牌", "—", "—", "控制权事项筹划"),
            ("2026-08-24~26", "连续一字涨停", "低于1%", "—", "强势但不可交易"),
        ],
        "event": "控制权变更事项触发停复牌和价格重估，事件确定性高于普通题材新闻。",
        "leakage_guard": "停牌期间标为deferred；复牌一字板单独标为unavailable，不把无法成交的账面涨幅计作策略收益。",
        "source": "巨潮资讯公司公告",
        "url": "https://www.cninfo.com.cn/new/disclosure",
    },
]


def render_historical_report(*, as_of: date = date(2026, 8, 27)) -> str:
    lines = [
        f"# Stock King 妖股雷达历史研究（截至 {as_of.isoformat()}）",
        "",
        "> 本报告是高风险异动研究，不构成投资建议。所有盘前结论必须按当时可见信息重建；上涨后的公告、新闻和修订数据不得回填。",
        "",
        "## 结论先行",
        "",
        "三只案例的共同点不是简单的‘涨停前放量’，而是量价状态、可传播题材/事件和市场承接同时跃迁。最重要的反例控制是可交易性：停牌和连续一字板即使涨幅巨大，也不能当作可复制收益。当前审计模型尚未通过五年滚动门禁，因此输出保持‘研究观察’，概率字段留空。",
        "",
        "| 案例 | 启动前结构 | 主要催化 | 最关键风险 |",
        "|---|---|---|---|",
    ]
    for case in CASE_STUDIES:
        lines.append(f"| {case['name']}（{case['code']}） | {case['thesis']} | {case['event']} | {case['leakage_guard']} |")
    for case in CASE_STUDIES:
        lines.extend([
            "",
            f"## {case['name']}（{case['code']}）",
            "",
            case["thesis"],
            "",
            "| 日期 | 涨跌 | 换手 | 量比 | 时点解释 |",
            "|---|---:|---:|---:|---|",
        ])
        for row in case["timeline"]:
            lines.append("| " + " | ".join(row) + " |")
        lines.extend([
            "",
            f"- 事件证据：{case['event']}",
            f"- 防泄漏规则：{case['leakage_guard']}",
            f"- 公告入口：[{case['source']}]({case['url']})",
        ])
    lines.extend([
        "",
        "## 共性特征与匹配反例",
        "",
        "| 维度 | 正样本应见 | 必须匹配的反例 |",
        "|---|---|---|",
        "| 量价动能 | 首次脉冲后承接、突破或反包 | 单日放量后迅速跌回区间 |",
        "| 换手结构 | 可交易换手逐步放大 | 一字板低换手应标不可交易，而非弱势 |",
        "| 波动状态 | 收缩后扩张或分歧转一致 | 高位纯波动、无新增承接 |",
        "| 题材/事件 | 热度加速度且与公司存在可核验联系 | 业务收入占比极低、仅名称联想 |",
        "| 信息时点 | 截止08:55已公开 | 上涨后公告、媒体复盘和修订数据 |",
        "| 风险 | 流动性允许人工执行 | 停牌、连续一字板、行情陈旧 |",
        "",
        "## 五年滚动验证门禁",
        "",
        "正式模型须在时间隔离样本外同时优于基线：PR-AUC更高、Brier更低、LogLoss更低、Lift@5更高，并通过最大回撤门槛。任一门槛未通过，只显示原始审计分和‘研究观察’，不显示伪精确概率。匹配反例按日期、板块、流通市值、前20日收益、波动率和流动性进行最近邻匹配。",
        "",
        "## 当前数据状态",
        "",
        "本报告固化三只回归样本及其防泄漏规则；近五年全市场样本由 `/api/v1/yao-scout/backfill/tasks` 分批构建并写入检查点。只有完整批次通过数据质量校验与模型门禁后，才会把 challenger 晋升为 champion。",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}（UTC）",
    ])
    return "\n".join(lines) + "\n"


def write_historical_report(output_dir: Path, *, as_of: date = date(2026, 8, 27)) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown = render_historical_report(as_of=as_of)
    md_path = output_dir / f"historical-research-{as_of.isoformat()}.md"
    html_path = output_dir / f"historical-research-{as_of.isoformat()}.html"
    md_path.write_text(markdown, encoding="utf-8")
    html_path.write_text(markdown_to_html_document(markdown), encoding="utf-8")
    return {"markdown": str(md_path.resolve()), "html": str(html_path.resolve())}


def append_validation_results(paths: dict[str, str], model: dict[str, Any]) -> None:
    """Append the latest concrete walk-forward gate evidence to both artifacts."""
    md_path = Path(paths["markdown"])
    text = md_path.read_text(encoding="utf-8")
    metrics = model.get("metrics") if isinstance(model.get("metrics"), dict) else {}
    gates = model.get("gates") if isinstance(model.get("gates"), dict) else {}
    lines = [
        "",
        "## 最近一次实际滚动验证",
        "",
        f"- 模型：`{model.get('model_version')}`；状态：`{model.get('status')}`；正式门禁：`{gates.get('qualified')}`。",
        f"- 样本：{metrics.get('sample_count', 0)}；训练：{metrics.get('train_count', 0)}；样本外：{metrics.get('test_count', 0)}；切分日：{metrics.get('split_date', '—')}。",
        "",
        "| 目标 | PR-AUC/基线 | Brier/基线 | LogLoss/基线 | Lift@5 | 选中样本平均最大回撤 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for label in ("ignition_3d", "continuation_5d", "strong_10d"):
        row = metrics.get(label) if isinstance(metrics.get(label), dict) else {}
        lines.append(
            f"| {label} | {row.get('pr_auc', '—')} / {row.get('baseline_pr_auc', '—')} | "
            f"{row.get('brier', '—')} / {row.get('baseline_brier', '—')} | "
            f"{row.get('log_loss', '—')} / {row.get('baseline_log_loss', '—')} | "
            f"{row.get('lift_at_5', '—')} | {row.get('mean_selected_max_drawdown_pct', '—')}% |"
        )
    matched = metrics.get("matched_counterexamples") if isinstance(metrics.get("matched_counterexamples"), dict) else {}
    lines.extend([
        "",
        f"匹配反例：{matched.get('matched_pairs', 0)} 对；中位标准化距离 {matched.get('median_standardized_distance', '—')}。本批次未通过正式门禁，因而不发布概率。",
        "",
    ])
    text += "\n".join(lines)
    md_path.write_text(text, encoding="utf-8")
    Path(paths["html"]).write_text(markdown_to_html_document(text), encoding="utf-8")
