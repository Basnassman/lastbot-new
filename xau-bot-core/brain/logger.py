"""
يسجل كل قرار (حتى لو "لا تداول") مع سببه الكامل.
لو لا تفهم ليش البوت اتخذ قراراً معيناً، هذا الملف يجب أن يجيبك دائماً.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "decisions.jsonl"


def log_decision(payload: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), **payload}
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
