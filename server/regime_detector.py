"""
regime_detector.py — Kantitatif Rejim Tespiti (V3.3)

HMM-tarzı rejim tespiti: volatilite clustering + getiri dağılımı + trend gücü
ile piyasa rejimini sayısal olarak belirler.

Claude AI'ın sübjektif rejim kararını kantitatif verilerle destekler/çelişir.

Rejimler:
  - bull_strong : Güçlü yükseliş (düşük vol, pozitif trend, yüksek momentum)
  - bull        : Yükseliş trendi
  - neutral     : Yatay/belirsiz
  - bear        : Düşüş trendi
  - bear_strong : Güçlü düşüş (yüksek vol, negatif trend, panik)

Çıktı:
  - regime: str
  - confidence: 0-100
  - components: {volatility_regime, trend_regime, momentum_regime, breadth}
  - reasoning: str
"""

import math
from datetime import datetime, timedelta, timezone

try:
    from config import BENCHMARK
except Exception:
    BENCHMARK = "XU100.IS"


def detect_regime(market_data: dict) -> dict:
    """
    Piyasa verisinden kantitatif rejim tespiti.

    market_data: market_scanner.get_market_data() çıktısı
    Returns: {regime, confidence, components, reasoning, quant_score}
    """
    meta = market_data.get("_meta", {})
    tickers = [k for k in market_data if k != "_meta"]

    if not tickers:
        return _default("Veri yok")

    # ─── 1. Volatilite Rejimi ─────────────────────────────────
    vol_score = _volatility_regime(market_data, tickers)

    # ─── 2. Trend Rejimi ─────────────────────────────────────
    trend_score = _trend_regime(market_data, tickers)

    # ─── 3. Momentum Rejimi ──────────────────────────────────
    mom_score = _momentum_regime(market_data, tickers)

    # ─── 4. Market Breadth (genişlik) ────────────────────────
    breadth = _market_breadth(market_data, tickers)

    # ─── Bileşik Skor (ağırlıklı) ────────────────────────────
    # Trend en önemli (%35), sonra momentum (%25), volatilite (%25), breadth (%15)
    composite = (
        trend_score["score"] * 0.35 +
        mom_score["score"] * 0.25 +
        vol_score["score"] * 0.25 +
        breadth["score"] * 0.15
    )

    # Rejim sınıflandırma
    if composite >= 65:
        regime = "bull_strong"
    elif composite >= 55:
        regime = "bull"
    elif composite >= 45:
        regime = "neutral"
    elif composite >= 35:
        regime = "bear"
    else:
        regime = "bear_strong"

    # Güven skoru: bileşenlerin ne kadar uyumlu olduğu
    scores = [trend_score["score"], mom_score["score"], vol_score["score"], breadth["score"]]
    avg = sum(scores) / len(scores)
    variance = sum((s - avg) ** 2 for s in scores) / len(scores)
    # Düşük varyans = yüksek güven (bileşenler uyumlu)
    confidence = max(20, min(95, int(100 - variance * 0.5)))

    reasoning = _build_reasoning(regime, composite, vol_score, trend_score, mom_score, breadth)

    return {
        "regime": regime,
        "quant_score": round(composite, 1),
        "confidence": confidence,
        "components": {
            "volatility": vol_score,
            "trend": trend_score,
            "momentum": mom_score,
            "breadth": breadth,
        },
        "reasoning": reasoning,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ─────────────────────────────────────────────────────────────────
# Bileşen Analizleri
# ─────────────────────────────────────────────────────────────────

def _volatility_regime(market_data: dict, tickers: list) -> dict:
    """
    Volatilite rejimi: ATR ve fiyat dağılımı bazlı.
    Düşük vol = bullish ortam, yüksek vol = belirsizlik/bearish.
    Score: 0-100 (yüksek = bullish)
    """
    atr_pcts = []
    for t in tickers:
        d = market_data.get(t, {})
        price = d.get("price", 0)
        atr = d.get("atr14", 0)
        if price > 0 and atr > 0:
            atr_pcts.append(atr / price * 100)

    if not atr_pcts:
        return {"score": 50, "avg_atr_pct": 0, "label": "unknown"}

    avg_atr = sum(atr_pcts) / len(atr_pcts)

    # ATR % normalizasyon: < 1% = çok düşük vol (bullish), > 4% = yüksek vol (bearish)
    if avg_atr <= 1.0:
        score = 80
        label = "low_volatility"
    elif avg_atr <= 1.5:
        score = 70
        label = "normal_low"
    elif avg_atr <= 2.5:
        score = 55
        label = "normal"
    elif avg_atr <= 3.5:
        score = 40
        label = "elevated"
    elif avg_atr <= 5.0:
        score = 25
        label = "high"
    else:
        score = 15
        label = "extreme"

    return {"score": score, "avg_atr_pct": round(avg_atr, 2), "label": label}


def _trend_regime(market_data: dict, tickers: list) -> dict:
    """
    Trend rejimi: EMA yapılanması + SPY/QQQ trend durumu.
    Score: 0-100 (yüksek = bullish)
    """
    trend_counts = {"strong_uptrend": 0, "uptrend": 0, "sideways": 0, "downtrend": 0, "strong_downtrend": 0}
    ema_alignments = []  # EMA9 > EMA21 > EMA50 alignment

    for t in tickers:
        d = market_data.get(t, {})
        trend = d.get("trend", "sideways")
        trend_counts[trend] = trend_counts.get(trend, 0) + 1

        ema9 = d.get("ema9", 0)
        ema21 = d.get("ema21", 0)
        ema50 = d.get("ema50", 0)
        price = d.get("price", 0)

        if price > 0 and ema9 > 0 and ema21 > 0 and ema50 > 0:
            # Perfect bullish alignment = +2, partial = +1, bearish = -1, perfect bearish = -2
            if price > ema9 > ema21 > ema50:
                ema_alignments.append(2)
            elif ema9 > ema21:
                ema_alignments.append(1)
            elif ema9 < ema21 < ema50 and price < ema50:
                ema_alignments.append(-2)
            elif ema9 < ema21:
                ema_alignments.append(-1)
            else:
                ema_alignments.append(0)

    total = len(tickers) or 1
    bullish_pct = (trend_counts.get("strong_uptrend", 0) + trend_counts.get("uptrend", 0)) / total * 100
    bearish_pct = (trend_counts.get("strong_downtrend", 0) + trend_counts.get("downtrend", 0)) / total * 100

    # EMA alignment ortalaması (-2 to +2 → 0-100)
    avg_alignment = sum(ema_alignments) / len(ema_alignments) if ema_alignments else 0
    alignment_score = (avg_alignment + 2) / 4 * 100  # Normalize to 0-100

    # Benchmark ağırlığı (BIST: XU100.IS)
    bench_data = market_data.get(BENCHMARK, {})
    bench_trend = bench_data.get("trend", "sideways")
    bench_bonus = {"strong_uptrend": 10, "uptrend": 5, "sideways": 0, "downtrend": -5, "strong_downtrend": -10}.get(bench_trend, 0)

    score = alignment_score * 0.6 + bullish_pct * 0.4 + bench_bonus
    score = max(0, min(100, score))

    label = "bullish" if bullish_pct > 60 else "bearish" if bearish_pct > 60 else "mixed"

    return {
        "score": round(score, 1),
        "bullish_pct": round(bullish_pct, 1),
        "bearish_pct": round(bearish_pct, 1),
        "avg_ema_alignment": round(avg_alignment, 2),
        "benchmark_trend": bench_trend,
        "label": label,
    }


def _momentum_regime(market_data: dict, tickers: list) -> dict:
    """
    Momentum rejimi: RSI dağılımı + momentum score ortalaması + MACD durumu.
    Score: 0-100 (yüksek = bullish momentum)
    """
    rsis = []
    momentum_scores = []
    macd_bullish = 0
    macd_bearish = 0

    for t in tickers:
        d = market_data.get(t, {})
        rsi = d.get("rsi14", 50)
        mom = d.get("momentum_score", 50)
        rsis.append(rsi)
        momentum_scores.append(mom)

        macd_cross = d.get("macd_cross", "none")
        macd_hist = d.get("macd_histogram", 0)
        if macd_cross == "bullish_cross" or macd_hist > 0:
            macd_bullish += 1
        elif macd_cross == "bearish_cross" or macd_hist < 0:
            macd_bearish += 1

    avg_rsi = sum(rsis) / len(rsis) if rsis else 50
    avg_mom = sum(momentum_scores) / len(momentum_scores) if momentum_scores else 50
    total = len(tickers) or 1
    macd_ratio = macd_bullish / total * 100

    # RSI score: 50-70 optimal bullish zone, <30 oversold bounce potential, >80 overheated
    if 50 <= avg_rsi <= 65:
        rsi_score = 75
    elif 40 <= avg_rsi < 50:
        rsi_score = 55
    elif avg_rsi < 30:
        rsi_score = 35  # Oversold — potential reversal
    elif avg_rsi > 75:
        rsi_score = 30  # Overbought risk
    else:
        rsi_score = 50

    score = avg_mom * 0.4 + rsi_score * 0.3 + macd_ratio * 0.3
    score = max(0, min(100, score))

    return {
        "score": round(score, 1),
        "avg_rsi": round(avg_rsi, 1),
        "avg_momentum": round(avg_mom, 1),
        "macd_bullish_pct": round(macd_ratio, 1),
        "label": "strong" if score >= 65 else "moderate" if score >= 45 else "weak",
    }


def _market_breadth(market_data: dict, tickers: list) -> dict:
    """
    Market breadth: kaç hisse yükselişte vs düşüşte.
    Advance/Decline ratio tarzı.
    Score: 0-100
    """
    advancing = 0
    declining = 0
    above_ema50 = 0

    for t in tickers:
        d = market_data.get(t, {})
        change = d.get("change_pct", 0)
        price = d.get("price", 0)
        ema50 = d.get("ema50", 0)

        if change > 0:
            advancing += 1
        elif change < 0:
            declining += 1

        if price > 0 and ema50 > 0 and price > ema50:
            above_ema50 += 1

    total = len(tickers) or 1
    adv_pct = advancing / total * 100
    above_ema50_pct = above_ema50 / total * 100

    # Advance/decline ratio → score
    ad_score = adv_pct  # Directly maps: 80% advancing = 80 score

    # EMA50 breadth
    ema_score = above_ema50_pct

    score = ad_score * 0.5 + ema_score * 0.5
    score = max(0, min(100, score))

    label = "strong" if score >= 70 else "healthy" if score >= 50 else "weak" if score >= 30 else "bearish"

    return {
        "score": round(score, 1),
        "advancing": advancing,
        "declining": declining,
        "above_ema50": above_ema50,
        "above_ema50_pct": round(above_ema50_pct, 1),
        "total": total,
        "label": label,
    }


# ─────────────────────────────────────────────────────────────────
# Yardımcılar
# ─────────────────────────────────────────────────────────────────

def _build_reasoning(regime, composite, vol, trend, mom, breadth) -> str:
    parts = []

    parts.append(f"Kantitatif skor: {composite:.1f}/100 → {regime.upper()}")

    # Trend (BIST benchmark = XU100)
    parts.append(f"Trend: %{trend['bullish_pct']:.0f} yükseliş, XU100 {trend.get('benchmark_trend', 'unknown')}")

    # Volatilite
    parts.append(f"Volatilite: {vol['label']} (ATR ort. %{vol['avg_atr_pct']})")

    # Momentum
    parts.append(f"Momentum: {mom['label']} (RSI ort. {mom['avg_rsi']:.0f}, MACD %{mom['macd_bullish_pct']:.0f} bullish)")

    # Breadth
    parts.append(f"Breadth: {breadth['advancing']}/{breadth['total']} yükselişte, %{breadth['above_ema50_pct']:.0f} EMA50 üstünde")

    return " | ".join(parts)


def _default(reason: str) -> dict:
    return {
        "regime": "neutral",
        "quant_score": 50,
        "confidence": 0,
        "components": {},
        "reasoning": reason,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
