#!/usr/bin/env python3
"""CLI entrypoint used by Windows Task Scheduler and manual verification."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.config import get_config  # noqa: E402
from src.services.yao_scout import YaoScoutService  # noqa: E402
from src.services.yao_scout.historical import write_historical_report  # noqa: E402
from src.storage import DatabaseManager  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stock King 妖股雷达")
    parser.add_argument("action", choices=("prefetch", "preopen", "postclose", "intraday", "backfill", "history-report", "test-email"))
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--notify", action="store_true")
    parser.add_argument("--allow-late", action="store_true")
    parser.add_argument("--symbols", default="", help="Backfill codes separated by commas")
    parser.add_argument("--years", type=int, default=5)
    parser.add_argument("--max-symbols", type=int, default=200)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = get_config()
    db = DatabaseManager.get_instance()
    service = YaoScoutService(config=config, db_manager=db)
    if args.action == "prefetch":
        result = service.prefetch()
    elif args.action in {"preopen", "postclose", "intraday"}:
        result = service.run(
            args.action,
            top_n=args.top,
            notify=args.notify,
            allow_late=args.allow_late,
        )
    elif args.action == "backfill":
        symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
        result = service.backfill(symbols=symbols, years=args.years, max_symbols=args.max_symbols)
    elif args.action == "history-report":
        result = write_historical_report(service.report_dir)
    else:
        content = (
            "# Stock King 妖股雷达测试邮件\n\n"
            f"发送时间：{datetime.now().isoformat()}\n\n"
            "如果收到此邮件，说明本机SMTP配置和收件地址可用。本邮件不含投资建议。\n"
        )
        if not getattr(config, "email_sender", None) or not getattr(config, "email_password", None):
            result = {"status": "config_missing", "message": "EMAIL_SENDER/EMAIL_PASSWORD 尚未配置，未尝试投递"}
        else:
            from src.notification import NotificationService

            success = NotificationService(config).send_to_email(content, subject="Stock King 妖股雷达｜SMTP测试")
            result = {"status": "sent" if success else "failed"}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") not in {"failed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
