"""Print unresolved trade-result audit records without changing the SQLite database.

Run from the project root:
    python -m scripts.report_unresolved_trade_results [--db PATH]

The command is for operator review only. It never applies or reconciles PnL.
"""
import argparse
import json

from brain.risk.trade_result_store import TradeResultStore
from config.settings import settings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=settings.trade_result_db_path, help="trade-result SQLite path")
    args = parser.parse_args()
    rows = TradeResultStore.read_unresolved(args.db)
    print(json.dumps({"count": len(rows), "items": rows}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
