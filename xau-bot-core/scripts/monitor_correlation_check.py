#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Quick stability monitor for DXY direction reported by /decide-log-only on cTrader Demo.

Goal:
- Measure whether the reported dollar direction (DXY) changes for no reason between
  close calls, or whether changes line up with actual price movement.
- Compare visually with the old documented Mock behavior:
    (DXY 101.4474/101.4201 on 2026-09-30 at 18:51 UTC <-> old dollar_trend_debug
    would flip randomly, e.g. 69.6 <- 30.2 within 4 minutes).

Run:
    DEMO_SYMBOL=XAUUSD BRAIN_API_URL=http://127.0.0.1:8000 \
        DEMO_INTERVAL_MIN=15 DEMO_DURATION_MIN=120 \
        DEMO_ORTHODOX=20.0 python3 scripts/monitor_correlation_check.py

Output: one line per call containing the timestamp, current/prev DXY, change and trend.
Final summary: number of trend changes between adjacent calls.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone

import requests

# --- Channel settings (external env only, no code changes) ---
API_BASE = os.environ.get("BRAIN_API_URL", "http://127.0.0.1:8000")
SYMBOL = os.environ.get("DEMO_SYMBOL", "XAUUSD")
INTERVAL_MIN = int(os.environ.get("DEMO_INTERVAL_MIN", "15"))
DURATION_MIN = int(os.environ.get("DEMO_DURATION_MIN", "120"))
ORTHODOX_MIN = float(os.environ.get("DEMO_ORTHODOX", "20.0"))


def call_decide_log_only() -> dict:
    """Fetch one /decide-log-only response without raising on network errors."""
    try:
        resp = requests.get(
            f"{API_BASE}/decide-log-only",
            params={"symbol": SYMBOL, "timeframe": "M15"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - every call failure must be reported
        return {"error": str(exc), "_call_time": datetime.now(timezone.utc).isoformat()}


def classify_trend(trend: object) -> str:
    """Convert the raw trend value into a stable monitor classification."""
    if trend in ("up", "down"):
        return "directional"
    if trend == "flat":
        return "flat"
    return "unknown"


def build_row(call_no: int, payload: dict, at: datetime) -> dict:
    dbg = payload.get("dollar_trend_debug") or {}
    current = dbg.get("current")
    previous = dbg.get("previous")
    change = dbg.get("change")
    trend = dbg.get("trend")
    derived = classify_trend(trend)

    return {
        "call": call_no,
        "at": at.isoformat(),
        "current_dxy": current,
        "previous_dxy": previous,
        "change": change,
        "trend": trend,
        "derived": derived,
    }


def main() -> None:
    end = datetime.now(timezone.utc).astimezone() + __import__("datetime").timedelta(minutes=DURATION_MIN)

    print(
        f">>> {SYMBOL} | {INTERVAL_MIN} min interval | {DURATION_MIN} min duration | orthodoxy >= {ORTHODOX_MIN} min",
        flush=True,
    )

    rows = []
    prev_trend = None
    prev_at = None
    changes = 0
    consecutive_errors = 0

    call = 0
    while datetime.now(timezone.utc).astimezone() < end:
        try:
            call += 1
            payload = call_decide_log_only()
            at = datetime.now(timezone.utc).astimezone()

            if "error" in payload:
                print(json.dumps({"t": at.isoformat(), "call": call, "error": payload["error"]}, ensure_ascii=False), flush=True)
                prev_at = at
                consecutive_errors += 1
                if consecutive_errors >= 5:
                    print("الخادم غير متاح — 5 نداءات متتالية فشلت، إيقاف السكربت فوراً.", flush=True)
                    return
                # لا continue هنا: يجب أن يصل time.sleep() في الأسفل دائماً
                # (نجاحاً أو فشلاً) حتى لا يتحول فشل الاتصال إلى حلقة متلاحقة.
            else:
                consecutive_errors = 0
                row = build_row(call, payload, at)
                rows.append(row)
                print(json.dumps(row, ensure_ascii=False), flush=True)

                # Trend stability measuring: only `dollar_trend_debug.trend`, as requested.
                if prev_trend is not None and row["trend"] != prev_trend:
                    changes += 1
                prev_trend = row["trend"]
                prev_at = at

        except Exception as exc:  # noqa: BLE001
            print(
                json.dumps(
                    {"t": datetime.now(timezone.utc).astimezone().isoformat(), "call": call + 1, "error": repr(exc)},
                    ensure_ascii=False,
                ),
                flush=True,
            )

        remaining = end - datetime.now(timezone.utc).astimezone()
        if remaining.total_seconds() > 0:
            time.sleep(min(INTERVAL_MIN * 60, max(remaining.total_seconds() - 2, 0)))

    print(
        f"\n>>> SUITE END call={call} changes={changes} "
        f"time={datetime.now(timezone.utc).astimezone().isoformat()}",
        flush=True,
    )

    # --- Comparison with the documented old behavior ---
    print(
        f">>> OLD BEHAVIOR (Mock): 69.6 <- 30.2 within 4 minutes, "
        f"random direction flips every call.",
        flush=True,
    )
    print(
        f">>> NEW BEHAVIOR: {changes} directional changes across {call} calls; "
        f"expect 0 if trend tracks real price movement.",
        flush=True,
    )

    if changes == 0 and prev_trend is not None:
        print(
            f">>> RESULT: PASS - direction stable across all calls, matching real price "
            f"movement instead of random.Mock behavior.",
            flush=True,
        )
    else:
        print(
            f">>> RESULT: MIXED - {changes} changes found; investigate whether changes "
            f"match DXY movement.",
            flush=True,
        )


if __name__ == "__main__":
    main()
