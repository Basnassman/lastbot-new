"""Dedicated unit tests for the isolated Phase 2 strategy module.

Scope: ONLY ``brain.strategies.trend_pullback_confirmation``. No network, no
broker, no database, no production logs. All OHLC data is synthetic and
deterministic. The shared autouse ``trade_store`` fixture from conftest points
the API's trade-result store at a temporary directory; it is imported but never
written to by these tests.
"""

from __future__ import annotations

import ast
import builtins
import io
import json
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from brain.analysis.indicators import ema, rsi
from brain.analysis.regime_detector import detect_regime
from brain.strategies.trend_pullback_confirmation import (
    M15_DURATION,
    TrendPullbackParams,
    _calculate_adx,
    _calculate_directional_movement,
    evaluate_trend_pullback_confirmation,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

H1_START = "2026-01-03T03:00:00Z"
M15_START = "2026-01-05T00:00:00Z"
# 51 M15 bars: 00:00 .. 12:30; reference time is the close of the last bar.
M15_LAST_TS = "2026-01-05T12:30:00Z"
REFERENCE = "2026-01-05T12:45:00Z"
H1_LAST_CLOSED_TS = "2026-01-05T11:00:00+00:00"


# --------------------------------------------------------------------------- #
# Deterministic synthetic OHLC builders
# --------------------------------------------------------------------------- #
def _ohlc_from_closes(closes, start=M15_START, freq="15min") -> pd.DataFrame:
    """Build valid OHLC candles: open = previous close, +/-0.5 wicks."""
    closes = [float(value) for value in closes]
    opens = [closes[0] - 0.5] + closes[:-1]
    highs = [max(open_, close) + 0.5 for open_, close in zip(opens, closes)]
    lows = [min(open_, close) - 0.5 for open_, close in zip(opens, closes)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(start, periods=len(closes), freq=freq, tz="UTC"),
            "open": opens,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100.0] * len(closes),
        }
    )


def _append_forming_candle(frame: pd.DataFrame, timestamp: str) -> pd.DataFrame:
    last_close = float(frame["close"].iloc[-1])
    row = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(timestamp),
                "open": last_close,
                "high": last_close + 0.5,
                "low": last_close - 0.5,
                "close": last_close + 0.1,
                "volume": 100.0,
            }
        ]
    )
    return pd.concat([frame, row], ignore_index=True)


def _h1_uptrend() -> pd.DataFrame:
    return _ohlc_from_closes([2400.0 + 2.0 * i for i in range(60)], H1_START, "1h")


def _h1_downtrend() -> pd.DataFrame:
    return _ohlc_from_closes([2400.0 - 2.0 * i for i in range(60)], H1_START, "1h")


def _h1_range() -> pd.DataFrame:
    return _ohlc_from_closes([2400.0] * 60, H1_START, "1h")


def _h1_weak_adx_uptrend() -> pd.DataFrame:
    """Closes drift up inside a fixed wide range: trend_up, but ADX == 0.

    Highs/lows are constant, so +DM and -DM are both zero while closes rise
    linearly; ``detect_regime`` still classifies trend_up.
    """
    closes = [2400.0 + 1.0 * i for i in range(60)]
    opens = [closes[0] - 0.5] + closes[:-1]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(H1_START, periods=60, freq="1h", tz="UTC"),
            "open": opens,
            "high": [2500.0] * 60,
            "low": [2380.0] * 60,
            "close": closes,
            "volume": [100.0] * 60,
        }
    )


def _m15_buy_closes() -> list[float]:
    """Oscillating drift, 3-bar pullback below EMA(21), bullish reclaim."""
    closes: list[float] = []
    close = 2400.0
    for index in range(47):
        close += 1.0 if index % 2 == 0 else -0.8
        closes.append(close)
    for _ in range(3):
        close -= 1.2
        closes.append(close)
    closes.append(close + 2.5)
    return closes


def _m15_buy() -> pd.DataFrame:
    return _ohlc_from_closes(_m15_buy_closes(), M15_START, "15min")


def _m15_sell() -> pd.DataFrame:
    """Mirror of the BUY fixture: bearish pullback + breakdown confirmation."""
    return _ohlc_from_closes(
        [4800.0 - value for value in _m15_buy_closes()], M15_START, "15min"
    )


def _evaluate(h1=None, m15=None, reference=REFERENCE, params=None):
    return evaluate_trend_pullback_confirmation(
        h1 if h1 is not None else _h1_uptrend(),
        m15 if m15 is not None else _m15_buy(),
        reference,
        params,
    )


# --------------------------------------------------------------------------- #
# 1-9: input validation
# --------------------------------------------------------------------------- #
def test_valid_data_produces_buy_setup():
    result = _evaluate()

    assert result.valid is True
    assert result.signal == "buy"
    assert result.reason == "signal_confirmed"
    assert result.trend_direction == "trend_up"
    assert result.pullback_detected is True
    assert result.confirmation_detected is True
    assert result.h1_bar_time == H1_LAST_CLOSED_TS
    assert result.m15_bar_time == "2026-01-05T12:30:00+00:00"
    assert result.valid_until == "2026-01-05T12:45:00+00:00"
    assert result.adx_value == pytest.approx(100.0, abs=1e-9)
    assert 40.0 <= result.rsi_value <= 65.0


@pytest.mark.parametrize(
    "column", ["timestamp", "open", "high", "low", "close", "volume"]
)
def test_missing_required_column_is_invalid(column):
    m15 = _m15_buy().drop(columns=[column])
    result = _evaluate(m15=m15)

    assert result.valid is False
    assert result.signal == "none"
    assert result.reason == "invalid_m15_data:missing_columns"


def test_nan_values_are_rejected():
    m15 = _m15_buy()
    m15.loc[m15.index[-1], "close"] = np.nan
    result = _evaluate(m15=m15)

    assert result.valid is False
    assert result.reason == "invalid_m15_data:nan_values"


def test_infinite_values_are_rejected():
    m15 = _m15_buy()
    m15.loc[m15.index[-1], "high"] = np.inf
    result = _evaluate(m15=m15)

    assert result.valid is False
    assert result.reason == "invalid_m15_data:infinite_values"


def test_invalid_ohlc_relationship_is_rejected():
    m15 = _m15_buy()
    m15.loc[m15.index[5], "high"] = float(m15.loc[m15.index[5], "low"]) - 1.0
    result = _evaluate(m15=m15)

    assert result.valid is False
    assert result.reason == "invalid_m15_data:invalid_ohlc_relationship"


def test_duplicate_timestamps_are_rejected():
    m15 = _m15_buy()
    m15.loc[m15.index[3], "timestamp"] = m15.loc[m15.index[2], "timestamp"]
    result = _evaluate(m15=m15)

    assert result.valid is False
    assert result.reason == "invalid_m15_data:duplicate_timestamps"


def test_unsorted_timestamps_are_rejected():
    m15 = _m15_buy()
    m15 = pd.concat(
        [m15.iloc[:10], m15.iloc[[12, 11]], m15.iloc[13:]], ignore_index=True
    )
    result = _evaluate(m15=m15)

    assert result.valid is False
    assert result.reason == "invalid_m15_data:unsorted_timestamps"


def test_insufficient_h1_history_is_invalid():
    result = _evaluate(h1=_h1_uptrend().head(20))

    assert result.valid is False
    assert result.reason == "insufficient_closed_h1_history"


def test_insufficient_m15_history_is_invalid():
    result = _evaluate(m15=_m15_buy().head(20))

    assert result.valid is False
    assert result.reason == "insufficient_closed_m15_history"


def test_naive_timestamps_are_rejected():
    m15 = _m15_buy()
    m15["timestamp"] = m15["timestamp"].dt.tz_localize(None)
    result = _evaluate(m15=m15)

    assert result.valid is False
    assert result.reason == "invalid_m15_data:naive_timestamps"


# --------------------------------------------------------------------------- #
# 10-14: closed-candle enforcement
# --------------------------------------------------------------------------- #
def test_forming_h1_candle_is_excluded():
    h1 = _append_forming_candle(_h1_uptrend(), "2026-01-05T15:00:00Z")
    result = _evaluate(h1=h1)

    assert result.valid is True
    assert result.signal == "buy"
    assert result.h1_bar_time == "2026-01-05T11:00:00+00:00"
    assert result.diagnostics["h1_forming_bars_excluded"] == 4


def test_forming_m15_candle_is_excluded():
    m15 = _append_forming_candle(_m15_buy(), "2026-01-05T12:45:00Z")
    result = _evaluate(m15=m15)

    assert result.valid is True
    assert result.signal == "buy"
    assert result.m15_bar_time == "2026-01-05T12:30:00+00:00"
    assert result.diagnostics["m15_forming_bars_excluded"] == 1


def test_candle_closing_exactly_at_reference_is_accepted():
    # REFERENCE is exactly the close of the final M15 candle (12:30 + 15m).
    result = _evaluate()

    assert result.valid is True
    assert result.diagnostics["m15_forming_bars_excluded"] == 0
    assert result.m15_bar_time == "2026-01-05T12:30:00+00:00"
    assert result.valid_until == REFERENCE.replace("Z", "+00:00")


def test_candle_closing_after_reference_is_rejected():
    # 5 minutes before the final bar closes: that bar is still forming.
    result = _evaluate(reference="2026-01-05T12:35:00Z")

    assert result.m15_bar_time == "2026-01-05T12:15:00+00:00"
    assert result.diagnostics["m15_forming_bars_excluded"] == 1


def test_insufficient_closed_history_returns_invalid():
    # Only ~10 H1 bars are closed at this early reference time.
    result = _evaluate(reference="2026-01-03T13:30:00Z")

    assert result.valid is False
    assert result.reason == "insufficient_closed_h1_history"


# --------------------------------------------------------------------------- #
# 15-20: indicator reuse and ADX
# --------------------------------------------------------------------------- #
def test_ema_reuse_matches_production_function():
    m15 = _m15_buy()
    result = _evaluate(m15=m15)

    expected = float(ema(m15["close"], 21).iloc[-1])
    assert result.diagnostics["ema_at_confirmation"] == pytest.approx(expected)


def test_rsi_reuse_matches_production_function():
    m15 = _m15_buy()
    result = _evaluate(m15=m15)

    expected = float(rsi(m15["close"], 14).iloc[-1])
    assert result.rsi_value == pytest.approx(expected)


def test_adx_calculation_produces_finite_values():
    adx = _calculate_adx(_h1_uptrend(), period=14)

    assert np.isfinite(adx.adx.iloc[-1])
    assert np.isfinite(adx.plus_di.iloc[-1])
    assert np.isfinite(adx.minus_di.iloc[-1])
    assert adx.plus_di.iloc[-1] > adx.minus_di.iloc[-1]
    assert adx.adx.iloc[-1] > 25.0


def test_plus_and_minus_di_behave_on_directional_data():
    up = _calculate_adx(_h1_uptrend(), period=14)
    down = _calculate_adx(_h1_downtrend(), period=14)

    assert up.plus_di.iloc[-1] > up.minus_di.iloc[-1]
    assert up.minus_di.iloc[-1] == pytest.approx(0.0)
    assert down.minus_di.iloc[-1] > down.plus_di.iloc[-1]
    assert down.plus_di.iloc[-1] == pytest.approx(0.0)


def test_zero_range_and_zero_denominator_are_safe():
    flat = pd.DataFrame(
        {
            "timestamp": pd.date_range(H1_START, periods=60, freq="1h", tz="UTC"),
            "open": [2400.0] * 60,
            "high": [2400.0] * 60,
            "low": [2400.0] * 60,
            "close": [2400.0] * 60,
            "volume": [100.0] * 60,
        }
    )
    adx = _calculate_adx(flat, period=14)

    assert adx.adx.iloc[-1] == 0.0
    assert adx.plus_di.iloc[-1] == 0.0
    assert adx.minus_di.iloc[-1] == 0.0
    assert np.all(np.isfinite(adx.adx.iloc[27:]))
    assert np.all(np.isfinite(adx.plus_di.iloc[14:]))


def test_adx_warmup_behavior():
    adx = _calculate_adx(_h1_uptrend(), period=14)

    # TR/DM smoothing is seeded at index 14; ADX is the average of the first 14
    # DX values, so the first ADX lands at index 2 * 14 - 1 == 27.
    assert adx.adx.first_valid_index() == 27
    assert adx.adx.iloc[:27].isna().all()
    assert adx.plus_di.first_valid_index() == 14

    short = _calculate_adx(_h1_uptrend().head(20), period=14)
    assert short.adx.isna().all()


def test_directional_movement_keeps_first_row_zero():
    plus_dm, minus_dm = _calculate_directional_movement(
        _h1_uptrend()["high"], _h1_uptrend()["low"]
    )

    assert plus_dm.iloc[0] == 0.0
    assert minus_dm.iloc[0] == 0.0
    assert (plus_dm.iloc[1:] >= 0).all()
    assert (minus_dm.iloc[1:] >= 0).all()


# --------------------------------------------------------------------------- #
# 21-30: strategy logic
# --------------------------------------------------------------------------- #
def test_valid_buy_setup():
    result = _evaluate()

    assert result.signal == "buy"
    assert result.trend_direction == "trend_up"
    assert result.diagnostics["confirmation_close"] > result.diagnostics["previous_high"]


def test_valid_sell_setup():
    result = _evaluate(h1=_h1_downtrend(), m15=_m15_sell())

    assert result.valid is True
    assert result.signal == "sell"
    assert result.trend_direction == "trend_down"
    assert result.pullback_detected is True
    assert result.confirmation_detected is True
    assert result.diagnostics["confirmation_close"] < result.diagnostics["previous_low"]


def test_h1_range_returns_none():
    result = _evaluate(h1=_h1_range())

    assert result.valid is True
    assert result.signal == "none"
    assert result.reason == "h1_range"
    assert result.trend_direction == "range"


def test_weak_adx_returns_none():
    h1 = _h1_weak_adx_uptrend()
    assert detect_regime(h1).regime == "trend_up"  # direction is up...
    result = _evaluate(h1=h1)

    assert result.valid is True
    assert result.signal == "none"
    assert result.reason == "adx_below_threshold"
    assert result.adx_value < 20.0


def test_missing_pullback_returns_none():
    m15 = _ohlc_from_closes([2400.0 + i for i in range(51)], M15_START, "15min")
    result = _evaluate(m15=m15)

    assert result.signal == "none"
    assert result.reason == "no_pullback"
    assert result.pullback_detected is False


def test_failed_ema_reclaim_returns_none():
    closes = _m15_buy_closes()
    closes[-1] = closes[-2] + 0.8  # weak bounce stays below EMA(21)
    result = _evaluate(m15=_ohlc_from_closes(closes, M15_START, "15min"))

    assert result.valid is True
    assert result.signal == "none"
    assert result.reason == "ema_not_reclaimed"
    assert result.pullback_detected is True
    assert result.confirmation_detected is False


def test_rsi_outside_band_returns_none():
    params = replace(TrendPullbackParams(), rsi_buy_max=50.0)
    result = _evaluate(params=params)

    assert result.signal == "none"
    assert result.reason == "rsi_out_of_band"
    assert result.rsi_value > 50.0


def test_failed_candle_confirmation_returns_none():
    m15 = _m15_buy()
    previous_index = m15.index[-2]
    # Raise the previous candle's high above the confirmation close while
    # keeping a valid candle and a genuine EMA reclaim.
    m15.loc[previous_index, "high"] = 2405.0
    result = _evaluate(m15=m15)

    assert result.valid is True
    assert result.signal == "none"
    assert result.reason == "confirmation_failed"
    assert result.pullback_detected is True
    assert result.confirmation_detected is False
    assert result.diagnostics["confirmation_close"] <= 2405.0


def test_never_simultaneous_buy_and_sell():
    buy_result = _evaluate()
    sell_result = _evaluate(h1=_h1_downtrend(), m15=_m15_sell())
    none_result = _evaluate(h1=_h1_range())

    assert buy_result.signal == "buy"
    assert sell_result.signal == "sell"
    assert none_result.signal == "none"
    for result in (buy_result, sell_result, none_result):
        assert result.signal in {"buy", "sell", "none"}


def test_old_confirmation_candle_cannot_produce_a_fresh_signal():
    # 30 minutes after the confirmation candle closed, a newer M15 bar would
    # have closed: the stored signal must not be presented as fresh.
    result = _evaluate(reference="2026-01-05T13:15:00Z")

    assert result.valid is True
    assert result.signal == "none"
    assert result.reason == "stale_confirmation_candle"
    assert result.signal_id is None
    assert result.valid_until is None


def test_signal_valid_until_is_end_of_confirmation_candle():
    result = _evaluate()

    expected = pd.Timestamp(result.m15_bar_time) + pd.Timedelta(M15_DURATION)
    assert result.valid_until == expected.isoformat()


# --------------------------------------------------------------------------- #
# 31-35: determinism and isolation
# --------------------------------------------------------------------------- #
def test_identical_inputs_produce_identical_outputs():
    first = _evaluate().to_dict()
    second = _evaluate().to_dict()

    assert first == second
    # Native-Python serialisability, including diagnostics.
    assert json.loads(json.dumps(first)) == first


def test_repeated_evaluation_keeps_signal_identity():
    first = _evaluate()
    second = _evaluate()

    assert first.signal_id is not None
    assert first.signal_id == second.signal_id
    assert first.signal_id == (
        "trend_pullback_confirmation:buy:2026-01-05T12:30:00+00:00"
    )
    assert first.m15_bar_time == second.m15_bar_time


def test_module_performs_no_file_writes(monkeypatch):
    def _forbidden_open(*args, **kwargs):
        raise AssertionError(f"unexpected file open: {args!r}")

    monkeypatch.setattr(builtins, "open", _forbidden_open)
    monkeypatch.setattr(io, "open", _forbidden_open)

    result = _evaluate()

    assert result.signal == "buy"


def test_module_makes_no_network_calls(monkeypatch):
    def _forbidden_socket(*args, **kwargs):
        raise AssertionError("unexpected network access")

    monkeypatch.setattr(socket, "socket", _forbidden_socket)
    monkeypatch.setattr(socket, "create_connection", _forbidden_socket)

    result = _evaluate()

    assert result.signal == "buy"


def test_module_does_not_import_forbidden_layers():
    source = (PROJECT_ROOT / "brain/strategies/trend_pullback_confirmation.py").read_text()
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_roots = {
        "fastapi",
        "ctrader_open_api",
        "brain.api",
        "brain.decision_engine",
        "brain.risk",
        "brain.scoring",
        "brain.data",
    }
    for module in imported:
        assert not any(
            module == root or module.startswith(root + ".") for root in forbidden_roots
        ), module

    allowed_roots = {
        "__future__",
        "ast",
        "math",
        "dataclasses",
        "datetime",
        "typing",
        "numpy",
        "pandas",
        "brain.analysis.indicators",
        "brain.analysis.regime_detector",
    }
    assert imported <= allowed_roots, imported - allowed_roots

    # Runtime check in a clean interpreter: none of the forbidden modules are
    # pulled in when importing the strategy.
    probe = (
        "import json, sys\n"
        "import brain.strategies.trend_pullback_confirmation\n"
        "roots = ['fastapi', 'ctrader_open_api', 'brain.api', "
        "'brain.decision_engine', 'brain.risk', 'brain.scoring', 'brain.data']\n"
        "bad = sorted(m for m in sys.modules "
        "if any(m == r or m.startswith(r + '.') for r in roots))\n"
        "print(json.dumps(bad))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == []
