"""
المرحلة 1 (البنية السوقية): كشف مناطق الدعم والمقاومة ومناطق السيولة المحتملة.

وحدة تحليل قابلة لإعادة الاستخدام، مبنية فوق نفس عقد PriceFeed (DataFrame
بالأعمدة timestamp/open/high/low/close/volume) الذي يستهلكه بقية الـBrain.
لا تعتمد على أي مصدر بيانات جديد ولا أي مكتبة خارجية (pandas/numpy فقط)، ولا
تُنشئ أو تُعدّل أو تُغلق أي صفقة — دورها تحليلي بحت.

مبادئ ثابتة:

1. نقاط الانعطاف (Swing) مؤكَّدة فقط: النقطة تحتاج ``swing_window`` شمعة على كل
   جانب لتثبت، وأي نقطة على حافة النافذة (تحتاج شموعاً مستقبلية للتأكيد) تُستبعد
   كلياً — فلا نظر إلى المستقبل (No look-ahead).

2. الحتمية (Determinism): نفس البيانات ونفس الإعدادات ⇒ نفس النتيجة بنفس
   الترتيب. لا عشوائية ولا اعتماد على ترتيب قواميس. الحقل الوحيد غير الحتمي هو
   ``analyzed_at`` (طابع زمني للتحليل) ويمكن حقنه صريحاً في الاختبارات.

3. الصمت ممنوع: أي بيانات ناقصة/تالفة/غير منتهية/غير مرتبة زمنياً ⇒ نتيجة
   ``valid=False`` بسبب موثَّق، وبلا مناطق مُختلقة.

4. فصل المسؤوليات: تحليل الدعم/المقاومة في ``analyze_market_structure``، وتحليل
   السيولة المحتملة في ``detect_liquidity_areas`` — منطقان منفصلان عمداً.

قيود صريحة (يجب عدم المبالغة في تفسيرها):
- "قوة المنطقة" مقياس تقديري (heuristic) وليست احتمال انعكاس مضموناً.
- "مناطق السيولة" مجرد مراجع سعرية محتملة؛ بيانات OHLC وحدها لا تُثبت وجود
  أوامر مؤسسية أو سيولة معلَّقة عند أي مستوى.
- لا تُستخدم معلومات الجلسة/المنطقة الزمنية إلا إن كانت أعمدة البيانات موثوقة؛
  الحدود "القريبة" هنا هي حدود النافذة المتاحة لا حدود جلسة تداول.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from brain.analysis.indicators import atr
from config.settings import settings

# --- أنواع المناطق والمراجع ---
SUPPORT = "support"
RESISTANCE = "resistance"

# --- ثوابت صيغة القوة (موثَّقة، مجموع الأوزان = 1.0) ---
_MAX_SCORED_REACTIONS = 5
_MAX_SCORED_TOUCHES = 5
_W_REACTIONS = 0.5
_W_RECENCY = 0.3
_W_TOUCHES = 0.2

# نسبة تداخل منطقتين تُعدّ فوقها المنطقة الأضعف تكراراً (تُهمَل)
_OVERLAP_DEDUPE_RATIO = 0.6
# سُمك أدنى لمنطقة أحادية المستوى (نسبة من تسامح الدمج)
_MIN_ZONE_WIDTH_FRACTION = 0.5
# عتبة كسر المنطقة (نسبة من مقياس التقلب scale)
_BREAK_BUFFER_FRACTION = 0.25
# تسامح تجميع المستويات المتشابهة لتحليل السيولة (نسبة من scale)
_LIQUIDITY_TOLERANCE_FRACTION = 0.15


@dataclass
class MarketStructure:
    """نتيجة تحليل البنية السوقية — يُحوَّل إلى dict قبل وضعه في استجابة الـAPI.

    الحقول اختيارية الأسلوب بحيث يبقى أي مستدعٍ قديم صالحاً دون تعديل.
    """

    valid: bool
    reason: str
    timeframe: str
    current_price: float | None
    support_zones: list[dict] = field(default_factory=list)
    resistance_zones: list[dict] = field(default_factory=list)
    liquidity_levels: list[dict] = field(default_factory=list)
    analyzed_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class _InvalidData(Exception):
    """مدخل غير صالح — يُترجم إلى نتيجة valid=False بسبب واضح (لا يُسقط الـBrain)."""


@dataclass(frozen=True)
class _Config:
    swing_window: int
    merge_tolerance_atr: float
    min_reactions: int
    max_zones: int
    recency_bars: int
    max_liquidity_levels: int


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def invalid_result(reason: str, timeframe: str = "M15", current_price: float | None = None,
                   analyzed_at: str | None = None) -> MarketStructure:
    """نتيجة صريحة عند تعذّر التحليل — تُستخدم أيضاً عند أي فشل غير متوقّع."""
    return MarketStructure(
        valid=False,
        reason=reason,
        timeframe=timeframe,
        current_price=current_price,
        analyzed_at=analyzed_at or _now_iso(),
    )


def _resolve_config(
    swing_window: int | None,
    merge_tolerance_atr: float | None,
    min_reactions: int | None,
    max_zones: int | None,
    recency_bars: int | None,
    max_liquidity_levels: int | None,
) -> _Config:
    """يقرأ الإعدادات عند الاستدعاء (لا وقت الاستيراد) مع قيم افتراضية آمنة."""
    return _Config(
        swing_window=max(1, int(settings.market_structure_swing_window if swing_window is None else swing_window)),
        merge_tolerance_atr=max(0.0, float(
            settings.market_structure_merge_tolerance_atr if merge_tolerance_atr is None else merge_tolerance_atr
        )),
        min_reactions=max(0, int(
            settings.market_structure_min_reactions if min_reactions is None else min_reactions
        )),
        max_zones=max(1, int(settings.market_structure_max_zones if max_zones is None else max_zones)),
        recency_bars=max(1, int(
            settings.market_structure_recency_bars if recency_bars is None else recency_bars
        )),
        max_liquidity_levels=max(1, int(
            settings.market_structure_max_liquidity_levels
            if max_liquidity_levels is None
            else max_liquidity_levels
        )),
    )


def _to_numeric(series: pd.Series, name: str) -> np.ndarray:
    """يحوّل عموداً إلى مصفوفة float — القيم غير الرقمية تصير NaN وتُرفَض لاحقاً."""
    try:
        array = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)
    except (TypeError, ValueError) as error:  # بنية عمود غير قابلة للتحويل
        raise _InvalidData(f"malformed_column:{name}") from error
    return array


def _validate(candles: pd.DataFrame) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """يتحقق من عقد البيانات ويعيد (open, high, low, close) كمصفوفات float.

    يرفض: غياب البيانات، غياب الأعمدة، القيم غير الرقمية/غير المنتهية، تعارض
    OHLC (high < low)، والطوابع الزمنية غير المرتبة تصاعدياً.
    """
    if candles is None:
        raise _InvalidData("missing_candles")
    if not isinstance(candles, pd.DataFrame):
        raise _InvalidData("candles_not_dataframe")
    if len(candles) == 0:
        raise _InvalidData("empty_candles")

    missing = [column for column in ("open", "high", "low", "close") if column not in candles.columns]
    if missing:
        raise _InvalidData("missing_columns:" + ",".join(missing))

    open_ = _to_numeric(candles["open"], "open")
    high = _to_numeric(candles["high"], "high")
    low = _to_numeric(candles["low"], "low")
    close = _to_numeric(candles["close"], "close")

    for name, array in (("open", open_), ("high", high), ("low", low), ("close", close)):
        if not np.isfinite(array).all():
            raise _InvalidData(f"non_finite_values:{name}")

    if np.any(high < low):
        raise _InvalidData("inconsistent_ohlc:high_below_low")

    if "timestamp" in candles.columns:
        try:
            if not candles["timestamp"].is_monotonic_increasing:
                raise _InvalidData("out_of_order_timestamps")
        except TypeError as error:
            raise _InvalidData("invalid_timestamps") from error

    return open_, high, low, close


def _volatility_scale(
    candles: pd.DataFrame, high: np.ndarray, low: np.ndarray, override: float | None = None
) -> float:
    """مقياس التقلب المستخدم للتسامح: ATR الحالي (تنفيذ موجود، غير معدَّل).

    ``override`` (اختياري) يُستخدم فقط عند تمرير قيمة ATR محسوبة مسبقاً (إعادة
    استخدام بلا إعادة حساب)، أو في الاختبارات لتحكم حتمي. بلا ATR صالح نستخدم
    وسيط المدى كبديل موثَّق بدل اختلاق قيمة. صفر أو أقل ⇒ لا مقياس ⇒ مدخل غير كافٍ.
    """
    if override is not None:
        value = float(override)
        if np.isfinite(value) and value > 0:
            return value
        raise _InvalidData("invalid_atr_override")

    try:
        atr_series = atr(candles)  # إعادة استخدام وليس استبدال
        value = float(atr_series.iloc[-1])
        if np.isfinite(value) and value > 0:
            return value
    except Exception:  # noqa: BLE001 — ننتقل للبديل الموثَّق عند أي فشل حساب
        pass

    if len(high) == 0:
        raise _InvalidData("no_volatility_scale")
    median_range = float(np.median(high - low))
    if np.isfinite(median_range) and median_range > 0:
        return median_range
    raise _InvalidData("no_volatility_scale")


def _swing_points(values: np.ndarray, window: int, find_high: bool) -> list[tuple[int, float]]:
    """نقاط انعطاف مؤكَّدة فقط (تتطلب ``window`` شمعة على كل جانب — لا مستقبل).

    الشرط صارم: القيمة يجب أن تكون أعلى/أخفض تماماً من كل شموع النافذة (بلا تعادل)
    كي تكون النتيجة حتمية ولا لبس فيها.
    """
    points: list[tuple[int, float]] = []
    n = len(values)
    if window < 1 or n < 2 * window + 1:
        return points

    for index in range(window, n - window):
        segment = values[index - window:index + window + 1]
        pivot = values[index]
        if find_high:
            if pivot >= segment.max() and int((segment == pivot).sum()) == 1:
                points.append((index, float(pivot)))
        else:
            if pivot <= segment.min() and int((segment == pivot).sum()) == 1:
                points.append((index, float(pivot)))
    return points


def _cluster_levels(levels: list[tuple[int, float]], tolerance: float) -> list[dict]:
    """يدمج المستويات المتقاربة (الفرق ≤ tolerance) في عناقيد حتمية مرتبة بالسعر."""
    ordered = sorted(levels, key=lambda item: (item[1], item[0]))
    clusters: list[dict] = []
    for index, price in ordered:
        if clusters and (price - clusters[-1]["max"]) <= tolerance:
            cluster = clusters[-1]
            cluster["min"] = min(cluster["min"], price)
            cluster["max"] = max(cluster["max"], price)
            cluster["members"].append((index, price))
        else:
            clusters.append({"min": price, "max": price, "members": [(index, price)]})
    return clusters


def _zone_from_cluster(
    cluster: dict,
    kind: str,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    scale: float,
    config: _Config,
) -> dict:
    """يبني منطقة واحدة من عنقود مستويات ويحسب قياساتها ودرجة قوتها."""
    members = cluster["members"]
    component_indices = [index for index, _price in members]
    zone_low = float(cluster["min"])
    zone_high = float(cluster["max"])

    # منطقة أحادية المستوى تصبح شريطاً رفيعاً حول السعر (بعرض موثَّق)
    min_width = max(_MIN_ZONE_WIDTH_FRACTION * config.merge_tolerance_atr * scale, scale * 1e-6)
    if zone_high - zone_low < min_width:
        middle = (zone_high + zone_low) / 2.0
        zone_low = middle - min_width / 2.0
        zone_high = middle + min_width / 2.0
    center = (zone_low + zone_high) / 2.0

    touches = 0
    reactions = 0
    last_reaction_index: int | None = None
    for index in range(len(close)):
        # تلامس: مدى الشمعة يتقاطع مع المنطقة
        if low[index] <= zone_high and high[index] >= zone_low:
            touches += 1
            if kind == SUPPORT and close[index] > zone_high:
                reactions += 1
                last_reaction_index = index
            elif kind == RESISTANCE and close[index] < zone_low:
                reactions += 1
                last_reaction_index = index

    # الكسر: إغلاق بعد تكوّن المنطقة يتجاوز حدّها البعيد بمقدار عتبة الكسر
    formed_index = max(component_indices)
    break_buffer = _BREAK_BUFFER_FRACTION * scale
    broken = False
    if kind == RESISTANCE:
        if bool(np.any(close[formed_index + 1:] > zone_high + break_buffer)):
            broken = True
    else:
        if bool(np.any(close[formed_index + 1:] < zone_low - break_buffer)):
            broken = True

    bars_since_reaction: int | None = None
    if last_reaction_index is not None:
        bars_since_reaction = (len(close) - 1) - last_reaction_index

    reaction_factor = min(reactions, _MAX_SCORED_REACTIONS) / _MAX_SCORED_REACTIONS
    touch_factor = min(touches, _MAX_SCORED_TOUCHES) / _MAX_SCORED_TOUCHES
    if bars_since_reaction is None:
        recency_factor = 0.0
    else:
        recency_factor = 1.0 - min(bars_since_reaction, config.recency_bars) / config.recency_bars
    strength = 100.0 * (
        _W_REACTIONS * reaction_factor
        + _W_RECENCY * recency_factor
        + _W_TOUCHES * touch_factor
    )

    return {
        "kind": kind,
        "low": round(zone_low, 6),
        "high": round(zone_high, 6),
        "center": round(center, 6),
        "strength": round(float(strength), 2),
        "reactions": int(reactions),
        "touches": int(touches),
        "last_reaction_bars_ago": None if bars_since_reaction is None else int(bars_since_reaction),
        "status": "broken" if broken else "active",
        "formed_index": int(formed_index),
    }


def _dedupe_overlapping(zones: list[dict]) -> list[dict]:
    """يحذف المناطق شديدة التداخل مع إبقاء الأقوى (ترتيب حتمي)."""
    kept: list[dict] = []
    ordered = sorted(zones, key=lambda zone: (-zone["strength"], -zone["reactions"], zone["center"]))
    for zone in ordered:
        duplicate = False
        for existing in kept:
            intersection = min(zone["high"], existing["high"]) - max(zone["low"], existing["low"])
            if intersection > 0:
                narrower = min(zone["high"] - zone["low"], existing["high"] - existing["low"])
                if narrower > 0 and intersection / narrower >= _OVERLAP_DEDUPE_RATIO:
                    duplicate = True
                    break
        if not duplicate:
            kept.append(zone)
    return kept


def _finalize_zones(zones: list[dict], current_price: float, config: _Config) -> list[dict]:
    """يرشّح بحد أدنى للتفاعلات، يضيف المسافة، ثم يرتّب ويحدّ العدد."""
    enriched: list[dict] = []
    for zone in zones:
        if zone["reactions"] < config.min_reactions:
            continue
        center = zone["center"]
        distance = abs(current_price - center)
        zone = dict(zone)
        zone["distance"] = round(float(distance), 6)
        zone["distance_pct"] = round(float(distance / current_price * 100.0), 4) if current_price else None
        enriched.append(zone)

    enriched = _dedupe_overlapping(enriched)
    # الترتيب: القوة تنازلياً ثم القرب من السعر تصاعدياً (حتمي)
    enriched.sort(key=lambda zone: (-zone["strength"], zone["distance"], zone["center"]))
    return enriched[: config.max_zones]


def detect_liquidity_areas(
    highs: np.ndarray,
    lows: np.ndarray,
    close: np.ndarray,
    scale: float,
    swing_highs: list[tuple[int, float]],
    swing_lows: list[tuple[int, float]],
    config: _Config,
    current_price: float,
) -> list[dict]:
    """مناطق سيولة محتملة (مراجع سعرية) — منطق منفصل عمداً عن الدعم/المقاومة.

    يشمل: تجمّعات القمم المتشابهة، تجمّعات القيعان المتشابهة، آخر قمة/قاع انعطاف
    مؤكَّدين، وأقصى/أدنى النافذة المتاحة. كلها مراجع محتملة لا إثبات لسيولة فعلية.
    """
    tolerance = max(_LIQUIDITY_TOLERANCE_FRACTION * scale, scale * 1e-6)
    areas: list[dict] = []

    def add(kind: str, price: float, count: int, note: str) -> None:
        distance = abs(current_price - price)
        areas.append(
            {
                "kind": kind,
                "price": round(float(price), 6),
                "count": int(count),
                "distance": round(float(distance), 6),
                "distance_pct": round(float(distance / current_price * 100.0), 4) if current_price else None,
                "note": note,
            }
        )

    # 1) تجمّعات القمم المتشابهة (Equal Highs) — سيولة محتملة أعلى السعر
    for cluster in _cluster_levels(swing_highs, tolerance):
        if len(cluster["members"]) >= 2:
            price = float(np.mean([value for _index, value in cluster["members"]]))
            add("equal_highs", price, len(cluster["members"]), "potential liquidity area (equal highs)")

    # 2) تجمّعات القيعان المتشابهة (Equal Lows) — سيولة محتملة أسفل السعر
    for cluster in _cluster_levels(swing_lows, tolerance):
        if len(cluster["members"]) >= 2:
            price = float(np.mean([value for _index, value in cluster["members"]]))
            add("equal_lows", price, len(cluster["members"]), "potential liquidity area (equal lows)")

    # 3) آخر قمة/قاع انعطاف مؤكَّد — مرجع سعري سابق
    if swing_highs:
        add("prior_swing_high", swing_highs[-1][1], 1, "price reference (prior confirmed swing high)")
    if swing_lows:
        add("prior_swing_low", swing_lows[-1][1], 1, "price reference (prior confirmed swing low)")

    # 4) أقصى/أدنى النافذة المتاحة — مرجع "طرف حديث" بلا افتراض جلسة
    add("recent_extreme_high", float(np.max(highs)), 1, "price reference (window extreme high)")
    add("recent_extreme_low", float(np.min(lows)), 1, "price reference (window extreme low)")

    # الترتيب: قوة التجمّع (count) تنازلياً ثم القرب تصاعدياً — حتمي
    areas.sort(key=lambda area: (-area["count"], area["distance"], area["kind"]))
    return areas[: config.max_liquidity_levels]


def analyze_market_structure(
    candles: pd.DataFrame,
    timeframe: str = "M15",
    *,
    swing_window: int | None = None,
    merge_tolerance_atr: float | None = None,
    min_reactions: int | None = None,
    max_zones: int | None = None,
    recency_bars: int | None = None,
    max_liquidity_levels: int | None = None,
    atr_value: float | None = None,
    analyzed_at: str | None = None,
) -> MarketStructure:
    """يحلّل البنية السوقية من شموع OHLC ويعيد مناطق دعم/مقاومة وسيولة محتملة.

    لا يرمي استثناءً على البيانات السيئة: يعيد ``valid=False`` بسبب موثَّق.
    ``analyzed_at`` قابل للحقن (للاختبارات وحتمية النتيجة).
    """
    config = _resolve_config(
        swing_window, merge_tolerance_atr, min_reactions, max_zones, recency_bars, max_liquidity_levels
    )
    timestamp = analyzed_at or _now_iso()

    try:
        open_, high, low, close = _validate(candles)
    except _InvalidData as invalid:
        return invalid_result(str(invalid), timeframe=timeframe, analyzed_at=timestamp)

    if len(close) < 2 * config.swing_window + 1:
        return invalid_result(
            f"insufficient_data:need_at_least_{2 * config.swing_window + 1}_candles",
            timeframe=timeframe,
            current_price=float(close[-1]) if len(close) else None,
            analyzed_at=timestamp,
        )

    try:
        scale = _volatility_scale(candles, high, low, override=atr_value)
    except _InvalidData as invalid:
        return invalid_result(
            str(invalid),
            timeframe=timeframe,
            current_price=float(close[-1]),
            analyzed_at=timestamp,
        )

    current_price = float(close[-1])

    swing_highs = _swing_points(high, config.swing_window, find_high=True)
    swing_lows = _swing_points(low, config.swing_window, find_high=False)

    resistance_zones = [
        _zone_from_cluster(cluster, RESISTANCE, high, low, close, scale, config)
        for cluster in _cluster_levels(swing_highs, config.merge_tolerance_atr * scale)
    ]
    support_zones = [
        _zone_from_cluster(cluster, SUPPORT, high, low, close, scale, config)
        for cluster in _cluster_levels(swing_lows, config.merge_tolerance_atr * scale)
    ]

    # حسم الترتيب النهائي مرّة واحدة لكل جانب (نفس الدالة، حتمية)
    final_resistance = _finalize_zones(resistance_zones, current_price, config)
    final_support = _finalize_zones(support_zones, current_price, config)

    liquidity = detect_liquidity_areas(
        high, low, close, scale, swing_highs, swing_lows, config, current_price
    )

    return MarketStructure(
        valid=True,
        reason="ok",
        timeframe=timeframe,
        current_price=current_price,
        support_zones=final_support,
        resistance_zones=final_resistance,
        liquidity_levels=liquidity,
        analyzed_at=timestamp,
    )
