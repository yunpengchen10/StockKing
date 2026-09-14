"""Security-metadata board classification shared by picks and tests."""
from __future__ import annotations

from typing import Any, Dict

from src.quant.service import normalize_a_share_symbol


def metadata_board(candidate: Dict[str, Any]) -> str:
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else {}
    for source in (candidate, raw, candidate.get("dsa_context") or {}):
        for key in ("board", "market", "market_name", "board_name", "exchange_segment"):
            value = str(source.get(key) or "").strip().lower()
            if "科创" in value or "star" in value:
                return "star"
            if "创业" in value or "chinext" in value:
                return "chinext"
            if "北交" in value or value == "bse":
                return "bse"
            if value in {"main", "主板"}:
                return "main"
    code = normalize_a_share_symbol(candidate.get("code") or "")
    if code.startswith(("688", "689")) and code.endswith(".SH"):
        return "star_code_fallback"
    if code.startswith("30") and code.endswith(".SZ"):
        return "chinext_code_fallback"
    if code.endswith(".BJ"):
        return "bse_code_fallback"
    return "main_code_fallback"


def board_group(candidate: Dict[str, Any]) -> str:
    return "kechuang" if metadata_board(candidate).startswith("star") else "non-kechuang"

