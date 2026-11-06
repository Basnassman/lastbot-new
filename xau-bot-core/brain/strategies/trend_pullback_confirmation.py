"""Trend Pullback Confirmation — standalone, deterministic XAUUSD strategy module.

PHASE 2 SCOPE: this module is an isolated analysis unit. It is NOT connected to
``DecisionEngine``, ``weighted_voting``, ``RiskManager``, the API, the executor,
or any price-feed / cTrader integration. It receives historical OHLC DataFrames
as arguments and returns a typed analysis result. It performs no I/O of any kind:
no network, no database, no files, no logging.

Strategy idea
-------------
- H1 sets the direction: ``detect_regime()`` (existing production function) must
  classify ``trend_up`` (BUY only) or ``trend_down`` (SELL only). A ``range``
  classification produces no signal.
- H1 trend strength must pass an experimental ADX(14) floor.
- M15 must show a pullback beyond EMA(21) and then a closed candle that reclaims
  the EMA, with a momentum candle body and a break of the previous candle's
  extreme.
- M15 RSI(14) must sit inside an experimental band.

Closed-candle enforcement
-------------------------
The caller must supply an explicit ``reference_time`` (the decision instant).
For each timeframe, a candle may only be used when

    candle_timestamp + timeframe_duration <= reference_time

so a forming candle is never used, even when it is the last row. Timestamps must
be timezone-aware; they are normalised to UTC.

Signal identity / expiry
------------------------
A signal is identified by the M15 confirmation candle: ``signal_id`` embeds the
direction and that candle timestamp, and ``valid_until`` is the end of the
confirmation candle. A signal whose confirmation candle has already closed before
the reference time is never returned as fresh; it degrades to
``signal="none"`` with reason ``stale_confirmation_candle``.

ADX is implemented privately here (Wilder smoothing) because the repository has
no ADX yet, and shared indicator files must not be modified.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal

import numpy as np
import pandas as pd

from brain.analysis.indicators import ema, rsi
from brain.analysis.regime_detector import detect_regime

Signal = Literal["buy", "sell", "none"]

H1_DURATION = timedelta(hours=1)
M15_DURATION = timedelta(minutes=15)
REQUIRED_COLUMNS = ("timestamp", "open", "high", "low", "close", "volume")

STRATEGY_NAME = "trend_pullback_confirmation"


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrendPullbackParams:
    """Function-level parameter set (experimental values, not live Brain settings).

    NOTE: these are intentionally separate from ``config/settings.py``. Nothing
    here modifies or overrides an existing project parameter.
    """

    # --- existing project parameter (detect_regime default, unchanged) ---
    regime_flat_threshold: float = 1.5

    # --- experimental (proposed by Phase 2, not proven profitable) ---
    adx_period: int = 14
    adx_min: float = 20.0
    ema_pullback_period: int = 21
    pullback_lookback: int = 10
    rsi_period: int = 14
    rsi_buy_min: float = 40.0
    rsi_buy_max: float = 65.0
    rsi_sell_min: float = 35.0
    rsi_sell_max: float = 60.0
    # --- safety floors (data sufficiency, not strategy edge) ---
    min_h1_closed_bars: int = 50
    min_m15_closed_bars: int = 50


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TrendPullbackResult:
    """Typed analysis result. All fields use native Python types."""

    valid: bool
    signal: Signal
    reason: str
    trend_direction: str | None = None
    trend_strength: float | None = None
    adx_value: float | None = None
    rsi_value: float | None = None
    pullback_detected: bool = False
    confirmation_detected: bool = False
    h1_bar_time: str | None = None
    m15_bar_time: str | None = None
    valid_until: str | None = None
    signal_id: str | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict built from native Python types."""
        return {
            "valid": bool(self.valid),
            "signal": self.signal,
            "reason": self.reason,
            "trend_direction": self.trend_direction,
            "trend_strength": self.trend_strength,
            "adx_value": self.adx_value,
            "rsi_value": self.rsi_value,
            "pullback_detected": bool(self.pullback_detected),
            "confirmation_detected": bool(self.confirmation_detected),
            "h1_bar_time": self.h1_bar_time,
            "m15_bar_time": self.m15_bar_time,
            "valid_until": self.valid_until,
            "signal_id": self.signal_id,
            "diagnostics": _to_native(self.diagnostics),
        }


@dataclass(frozen=True)
class AdxResult:
    """Private ADX output (SI units of percentage points)."""

    adx: pd.Series
    plus_di: pd.Series
    minus_di: pd.Series


def _to_native(value: Any) -> Any:
    """Recursively convert numpy / pandas scalars into native Python types."""
    if isinstance(value, dict):
        return {str(k): _to_native(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_native(v) for v in value]
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    return value


class _CandleDataError(Exception):
    """Explicit data-validation failure carrying a machine-readable reason."""

    def __init__(self, label: str, code: str, detail: str = "") -> None:
        self.code = code
        self.label = label
        self.detail = detail
        super().__init__(f"{label}:{code}")

    @property
    def reason(self) -> str:
        return f"invalid_{self.label}_data:{self.code}"


# --------------------------------------------------------------------------- #
# Input validation
# --------------------------------------------------------------------------- #
def _validate_candles(candles: pd.DataFrame, label: str) -> pd.DataFrame:
    """Validate and normalise one timeframe. Raises ``_CandleDataError``.

    Validation order is fixed and deterministic: columns -> rows -> timestamps ->
    numeric values -> NaN -> infinity -> OHLC relationships -> duplicates ->
    chronological ordering.
    """
    if not isinstance(candles, pd.DataFrame):
        raise _CandleDataError(label, "not_a_dataframe", type(candles).__name__)

    missing = [column for column in REQUIRED_COLUMNS if column not in candles.columns]
    if missing:
        raise _CandleDataError(label, "missing_columns", ",".join(missing))

    if len(candles) == 0:
        raise _CandleDataError(label, "empty_data")

    frame = candles.loc[:, list(REQUIRED_COLUMNS)].copy()
    frame = frame.reset_index(drop=True)

    # --- timestamps: must be present, timezone-aware, and convertible ---
    timestamps: list[pd.Timestamp] = []
    for value in frame["timestamp"]:
        try:
            stamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise _CandleDataError(label, "invalid_timestamps", str(exc)) from exc
        if pd.isna(stamp):
            raise _CandleDataError(label, "invalid_timestamps", "NaT")
        if stamp.tzinfo is None:
            raise _CandleDataError(label, "naive_timestamps", "expected tz-aware UTC")
        timestamps.append(stamp.tz_convert("UTC"))
    frame["timestamp"] = pd.Series(timestamps)

    # --- numeric OHLC / volume ---
    numeric_columns = ("open", "high", "low", "close", "volume")
    for column in numeric_columns:
        raw = frame[column]
        if raw.isna().any():
            raise _CandleDataError(label, "nan_values", column)
        if raw.dtype == object or not pd.api.types.is_numeric_dtype(raw):
            coerced = pd.to_numeric(raw, errors="coerce")
            if coerced.isna().any():
                raise _CandleDataError(label, "non_numeric_values", column)
            frame[column] = coerced.astype(float)
        else:
            frame[column] = raw.astype(float)

    for column in numeric_columns:
        values = frame[column].to_numpy(dtype=float)
        if not np.all(np.isfinite(values)):
            raise _CandleDataError(label, "infinite_values", column)

    # --- impossible candles ---
    body_high = np.maximum(frame["open"], frame["close"])
    body_low = np.minimum(frame["open"], frame["close"])
    invalid_high = frame["high"] < body_high
    invalid_low = frame["low"] > body_low
    inverted = frame["high"] < frame["low"]
    if bool(invalid_high.any() or invalid_low.any() or inverted.any()):
        raise _CandleDataError(label, "invalid_ohlc_relationship")

    # --- duplicate timestamps then ordering ---
    if frame["timestamp"].duplicated().any():
        raise _CandleDataError(label, "duplicate_timestamps")
    if not frame["timestamp"].is_monotonic_increasing:
        raise _CandleDataError(label, "unsorted_timestamps")

    return frame


def _filter_closed(
    frame: pd.DataFrame, duration: timedelta, reference_time: pd.Timestamp
) -> tuple[pd.DataFrame, int]:
    """Keep only candles fully closed at ``reference_time``."""
    close_times = frame["timestamp"] + pd.Timedelta(duration)
    mask = close_times <= reference_time
    closed = frame.loc[mask].reset_index(drop=True)
    excluded = int((~mask).sum())
    return closed, excluded


def _resolve_reference_time(value: Any) -> pd.Timestamp | None:
    """Parse the decision instant as an aware UTC timestamp (or None)."""
    try:
        stamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if pd.isna(stamp) or stamp.tzinfo is None:
        return None
    return stamp.tz_convert("UTC")


def _iso(stamp: pd.Timestamp) -> str:
    return stamp.isoformat()


# --------------------------------------------------------------------------- #
# Private Wilder ADX / DI implementation
# --------------------------------------------------------------------------- #
def _calculate_true_range(
    high: pd.Series, low: pd.Series, close: pd.Series
) -> pd.Series:
    """True Range = max(high-low, |high-prev_close|, |low-prev_close|)."""
    prev_close = close.shift(1)
    return pd.concat(
        [
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _calculate_directional_movement(
    high: pd.Series, low: pd.Series
) -> tuple[pd.Series, pd.Series]:
    """Wilder +DM / -DM.

    +DM = max(high - prev_high, 0) when it exceeds prev_low - low, else 0.
    -DM = max(prev_low - low, 0) when it exceeds high - prev_high, else 0.
    The first row has no previous candle, so both are 0.
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where(
            (up_move > down_move) & (up_move > 0),
            up_move.fillna(0.0),
            0.0,
        ),
        index=high.index,
        dtype=float,
    )
    minus_dm = pd.Series(
        np.where(
            (down_move > up_move) & (down_move > 0),
            down_move.fillna(0.0),
            0.0,
        ),
        index=low.index,
        dtype=float,
    )
    return plus_dm, minus_dm


def _wilder_smooth(values: pd.Series, period: int) -> pd.Series:
    """Wilder smoothing with *sum* initialisation.

    The seed at index ``period`` is the sum of the first ``period`` movements
    (indices 1..period; index 0 has no previous candle). Each following value is
    ``prev - prev/period + current``, which is Wilder's recursive form. Because
    +DI/-DI are ratios of identically smoothed series, the sum (rather than
    average) initialisation leaves them unchanged and is the conventional form.
    """
    array = values.to_numpy(dtype=float)
    out = np.full(array.shape, np.nan, dtype=float)
    if period <= 0 or len(array) <= period:
        return pd.Series(out, index=values.index)

    out[period] = float(np.nansum(array[1 : period + 1]))
    for index in range(period + 1, len(array)):
        previous = out[index - 1]
        current = 0.0 if math.isnan(array[index]) else array[index]
        out[index] = previous - previous / period + current
    return pd.Series(out, index=values.index)


def _calculate_adx(
    candles: pd.DataFrame, period: int = 14
) -> AdxResult:
    """Wilder ADX / +DI / -DI.

    Returns NaN during warm-up. The first ADX needs ``2 * period`` bars: DX is
    first defined once smoothed TR/DM exist (index ``period``), and ADX is the
    Wilder average of the first ``period`` DX values, so it lands at
    ``2 * period - 1``. Zero denominators (flat market, zero range) produce
    finite 0.0 values instead of NaN/inf.
    """
    high = candles["high"].astype(float)
    low = candles["low"].astype(float)
    close = candles["close"].astype(float)

    true_range = _calculate_true_range(high, low, close)
    plus_dm, minus_dm = _calculate_directional_movement(high, low)

    smoothed_tr = _wilder_smooth(true_range, period)
    smoothed_plus = _wilder_smooth(plus_dm, period)
    smoothed_minus = _wilder_smooth(minus_dm, period)

    tr_values = smoothed_tr.to_numpy(dtype=float)
    plus_values = smoothed_plus.to_numpy(dtype=float)
    minus_values = smoothed_minus.to_numpy(dtype=float)
    denom_valid = np.isfinite(tr_values)

    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di_values = np.where(
            tr_values > 0, 100.0 * plus_values / tr_values, 0.0
        )
        minus_di_values = np.where(
            tr_values > 0, 100.0 * minus_values / tr_values, 0.0
        )
        di_sum = plus_di_values + minus_di_values
        dx_values = np.where(
            di_sum > 0,
            100.0 * np.abs(plus_di_values - minus_di_values) / di_sum,
            0.0,
        )

    plus_di = pd.Series(plus_di_values, index=candles.index).where(denom_valid)
    minus_di = pd.Series(minus_di_values, index=candles.index).where(denom_valid)
    dx = pd.Series(dx_values, index=candles.index).where(denom_valid)

    dx_array = dx.to_numpy(dtype=float)
    adx_values = np.full(len(dx), np.nan, dtype=float)
    first_adx_index = 2 * period - 1
    if period > 0 and len(dx) > first_adx_index:
        seed = dx_array[period : first_adx_index + 1]
        if len(seed) == period and np.all(np.isfinite(seed)):
            adx_values[first_adx_index] = float(seed.mean())
            for index in range(first_adx_index + 1, len(dx)):
                current = dx_array[index]
                if np.isfinite(current) and np.isfinite(adx_values[index - 1]):
                    adx_values[index] = (
                        adx_values[index - 1] * (period - 1) + current
                    ) / period

    return AdxResult(
        adx=pd.Series(adx_values, index=candles.index),
        plus_di=plus_di,
        minus_di=minus_di,
    )


# --------------------------------------------------------------------------- #
# Result helpers
# --------------------------------------------------------------------------- #
def _invalid(reason: str, detail: str = "") -> TrendPullbackResult:
    diagnostics: dict[str, Any] = {"strategy": STRATEGY_NAME}
    if detail:
        diagnostics["detail"] = detail
    return TrendPullbackResult(
        valid=False,
        signal="none",
        reason=reason,
        diagnostics=diagnostics,
    )


# --------------------------------------------------------------------------- #
# Strategy evaluation
# --------------------------------------------------------------------------- #
def evaluate_trend_pullback_confirmation(
    h1_candles: pd.DataFrame,
    m15_candles: pd.DataFrame,
    reference_time: Any,
    params: TrendPullbackParams | None = None,
) -> TrendPullbackResult:
    """Evaluate the Trend Pullback Confirmation setup.

    Pure function: deterministic for identical inputs, no I/O, no global state.
    Invalid data or calculation failures return ``valid=False`` with an explicit
    reason; a well-formed market without a setup returns ``valid=True`` with
    ``signal="none"`` and the failed condition as the reason.
    """
    settings = params or TrendPullbackParams()

    reference = _resolve_reference_time(reference_time)
    if reference is None:
        return _invalid(
            "invalid_reference_time", "reference_time must be a tz-aware timestamp"
        )

    try:
        h1 = _validate_candles(h1_candles, "h1")
        m15 = _validate_candles(m15_candles, "m15")
    except _CandleDataError as exc:
        return _invalid(exc.reason, exc.detail)

    h1_closed, h1_excluded = _filter_closed(h1, H1_DURATION, reference)
    m15_closed, m15_excluded = _filter_closed(m15, M15_DURATION, reference)

    diagnostics: dict[str, Any] = {
        "strategy": STRATEGY_NAME,
        "h1_closed_bars": int(len(h1_closed)),
        "m15_closed_bars": int(len(m15_closed)),
        "h1_forming_bars_excluded": h1_excluded,
        "m15_forming_bars_excluded": m15_excluded,
        "reference_time": _iso(reference),
        "params": _to_native(settings.__dict__),
    }

    if len(h1_closed) < settings.min_h1_closed_bars:
        return TrendPullbackResult(
            valid=False,
            signal="none",
            reason="insufficient_closed_h1_history",
            diagnostics=diagnostics,
        )
    if len(m15_closed) < settings.min_m15_closed_bars:
        return TrendPullbackResult(
            valid=False,
            signal="none",
            reason="insufficient_closed_m15_history",
            diagnostics=diagnostics,
        )

    h1_bar_time = _iso(h1_closed["timestamp"].iloc[-1])
    m15_bar_time = _iso(m15_closed["timestamp"].iloc[-1])
    diagnostics["h1_bar_time"] = h1_bar_time
    diagnostics["m15_bar_time"] = m15_bar_time

    # --- indicator calculations (isolated: any failure is an invalid result) ---
    try:
        regime = detect_regime(
            h1_closed, flat_threshold=settings.regime_flat_threshold
        )
        adx_result = _calculate_adx(h1_closed, period=settings.adx_period)
        adx_value = float(adx_result.adx.iloc[-1])
        plus_di = float(adx_result.plus_di.iloc[-1])
        minus_di = float(adx_result.minus_di.iloc[-1])
        if not math.isfinite(adx_value):
            raise ValueError("adx_not_finite")

        m15_ema = ema(m15_closed["close"], settings.ema_pullback_period)
        m15_rsi = rsi(m15_closed["close"], settings.rsi_period)
        rsi_value = float(m15_rsi.iloc[-1])
        if not math.isfinite(rsi_value):
            raise ValueError("rsi_not_finite")
    except Exception as exc:  # noqa: BLE001 - isolated module must fail safely
        diagnostics["error"] = f"{type(exc).__name__}: {exc}"
        return TrendPullbackResult(
            valid=False,
            signal="none",
            reason="indicator_calculation_failed",
            diagnostics=diagnostics,
        )

    confirmation_index = len(m15_closed) - 1
    ema_at_confirmation = float(m15_ema.iloc[confirmation_index])
    confirmation = m15_closed.iloc[confirmation_index]
    previous = m15_closed.iloc[confirmation_index - 1]

    diagnostics.update(
        {
            "regime_slope": float(regime.slope_value),
            "regime_confirmation_reference": _to_native(
                regime.confirmation_reference
            ),
            "ema_at_confirmation": ema_at_confirmation,
            "plus_di": plus_di,
            "minus_di": minus_di,
            "confirmation_close": float(confirmation["close"]),
            "confirmation_open": float(confirmation["open"]),
            "previous_high": float(previous["high"]),
            "previous_low": float(previous["low"]),
        }
    )

    def result(
        signal: Signal,
        reason: str,
        *,
        pullback_detected: bool,
        confirmation_detected: bool,
        valid_until: str | None = None,
        signal_id: str | None = None,
    ) -> TrendPullbackResult:
        return TrendPullbackResult(
            valid=True,
            signal=signal,
            reason=reason,
            trend_direction=regime.regime,
            trend_strength=adx_value,
            adx_value=adx_value,
            rsi_value=rsi_value,
            pullback_detected=pullback_detected,
            confirmation_detected=confirmation_detected,
            h1_bar_time=h1_bar_time,
            m15_bar_time=m15_bar_time,
            valid_until=valid_until,
            signal_id=signal_id,
            diagnostics=dict(diagnostics, signal_reason=reason),
        )

    # --- A. H1 trend direction ---
    if regime.regime == "range":
        return result(
            "none", "h1_range", pullback_detected=False, confirmation_detected=False
        )

    direction: Literal["buy", "sell"] = (
        "buy" if regime.regime == "trend_up" else "sell"
    )

    # --- B. H1 ADX floor ---
    if adx_value < settings.adx_min:
        return result(
            "none",
            "adx_below_threshold",
            pullback_detected=False,
            confirmation_detected=False,
        )

    # --- C. M15 pullback beyond the EMA, strictly before the confirmation ---
    window_start = max(0, confirmation_index - settings.pullback_lookback)
    pullback_index: int | None = None
    for index in range(confirmation_index - 1, window_start - 1, -1):
        if direction == "buy":
            if m15_closed["low"].iloc[index] < m15_ema.iloc[index]:
                pullback_index = index
                break
        else:
            if m15_closed["high"].iloc[index] > m15_ema.iloc[index]:
                pullback_index = index
                break

    if pullback_index is None:
        return result(
            "none", "no_pullback", pullback_detected=False, confirmation_detected=False
        )

    diagnostics["pullback_index"] = int(pullback_index)
    diagnostics["pullback_bar_time"] = _iso(m15_closed["timestamp"].iloc[pullback_index])
    diagnostics["ema_at_pullback"] = float(m15_ema.iloc[pullback_index])

    # --- C/D. EMA reclaim ---
    close_price = float(confirmation["close"])
    if direction == "buy":
        reclaimed = close_price > ema_at_confirmation
    else:
        reclaimed = close_price < ema_at_confirmation
    if not reclaimed:
        return result(
            "none",
            "ema_not_reclaimed",
            pullback_detected=True,
            confirmation_detected=False,
        )

    # --- E. Candle confirmation (closed candle, body, previous extreme) ---
    open_price = float(confirmation["open"])
    if direction == "buy":
        confirmation_ok = (
            close_price > open_price and close_price > float(previous["high"])
        )
    else:
        confirmation_ok = (
            close_price < open_price and close_price < float(previous["low"])
        )
    if not confirmation_ok:
        return result(
            "none",
            "confirmation_failed",
            pullback_detected=True,
            confirmation_detected=False,
        )

    # --- D. RSI band ---
    if direction == "buy":
        rsi_ok = settings.rsi_buy_min <= rsi_value <= settings.rsi_buy_max
    else:
        rsi_ok = settings.rsi_sell_min <= rsi_value <= settings.rsi_sell_max
    if not rsi_ok:
        return result(
            "none",
            "rsi_out_of_band",
            pullback_detected=True,
            confirmation_detected=True,
        )

    # --- G. Expiry: the signal ends with its confirmation candle ---
    valid_until_stamp = m15_closed["timestamp"].iloc[confirmation_index] + pd.Timedelta(
        M15_DURATION
    )
    if reference > valid_until_stamp:
        return result(
            "none",
            "stale_confirmation_candle",
            pullback_detected=True,
            confirmation_detected=True,
        )

    signal_id = f"{STRATEGY_NAME}:{direction}:{m15_bar_time}"
    return result(
        direction,
        "signal_confirmed",
        pullback_detected=True,
        confirmation_detected=True,
        valid_until=_iso(valid_until_stamp),
        signal_id=signal_id,
    )
