"""
market_scanner.py — İki Aşamalı BIST Piyasa Tarayıcı (Meridian TR V1)

Mimari:
  EVRE 1 (Wide Scan — ucuz):
    - yfinance batch download: ~380 BIST ticker × 90 gün OHLCV
    - Her ticker için SADECE basit metrikler: change_pct, gap_pct,
      volume_ratio, basic_momentum_score, avg_vol_try (likidite)
    - Likidite filtresi: MIN_LIQUIDITY_TRY altı atlanır
    - Pre-filter eşikleri (volume_ratio, momentum, change) ile
      Top N aday seçilir

  EVRE 2 (Narrow Focus — pahalı):
    - Evren: Focus List (WATCHLIST) ∪ Top N adaylar
    - Her hisse için tüm indikatörler: EMA, RSI, ATR, MACD, Bollinger,
      VWAP, trend, signal, momentum_score (V3 full)
    - Claude bu zenginleştirilmiş veriyle karar verir

Hesaplamalar saf Python — harici ML/TA kütüphanesi gerekmez.
"""

import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from dotenv import load_dotenv

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

from config import (
    WATCHLIST as _FOCUS_LIST,
    BENCHMARK,
    LOOKBACK_DAYS,
    SIGNAL_GAP_THRESHOLD, SIGNAL_VOLUME_THRESHOLD,
    SCAN_UNIVERSE,
    PREFILTER_TOP_N, PREFILTER_MIN_VOLUME_RATIO,
    PREFILTER_MIN_MOMENTUM_SCORE, PREFILTER_MIN_ABS_CHANGE,
    MIN_LIQUIDITY_TRY,
    MARKET_TZ,
)
from universe_loader import get_universe, get_focus_list

# ─────────────────────────────────────────────────────────────────

def get_market_data() -> dict:
    """
    İki aşamalı BIST taraması.

    Returns:
        {
          "AKBNK.IS": { "price": 42.1, ..., "signal": "buy", "momentum_score": 72 },
          ...,
          "_meta": {
            "market_open": true,
            "scan_time": "...",
            "regime": "bull",
            "benchmark_change": 1.2,
            "total_scanned": 380,
            "stage1_passed": 42,
            "stage2_analyzed": 55,
            "bullish_count": 8,
          }
        }
    """
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np

        # ─── EVRE 1: Wide scan ────────────────────────────────────
        universe = get_universe() if SCAN_UNIVERSE != "watchlist" else get_focus_list()
        focus_list = get_focus_list()

        # Benchmark'ı universe'e ekle (rejim algılama için lazım)
        if BENCHMARK and BENCHMARK not in universe:
            universe = universe + [BENCHMARK]

        end   = datetime.now(timezone.utc)
        start = end - timedelta(days=LOOKBACK_DAYS)

        # Batch download — yfinance threads=True paralel çeker
        try:
            df = yf.download(
                tickers=" ".join(universe),
                start=start.strftime("%Y-%m-%d"),
                end=end.strftime("%Y-%m-%d"),
                interval="1d",
                group_by="ticker",
                threads=True,
                progress=False,
                auto_adjust=True,
            )
        except Exception as e:
            return {"error": f"yfinance download basarisiz: {e}"}

        # Evre 1 hesaplamalari
        stage1_metrics: dict = {}   # ticker → {change_pct, gap_pct, vol_ratio, basic_momentum, avg_vol_try}
        benchmark_change = 0.0
        benchmark_price  = 0.0

        for ticker in universe:
            try:
                # yfinance multi-ticker df: df[ticker][field]
                if isinstance(df.columns, pd.MultiIndex):
                    if ticker not in df.columns.get_level_values(0):
                        continue
                    t_df = df[ticker].dropna(how="all")
                else:
                    # tek ticker varsa flat cols
                    t_df = df.dropna(how="all")

                if len(t_df) < 5:
                    continue

                closes  = t_df["Close"].values.astype(float)
                opens   = t_df["Open"].values.astype(float)
                volumes = t_df["Volume"].values.astype(float)

                # NaN temizle
                if np.isnan(closes[-1]) or np.isnan(closes[-2]):
                    continue

                current    = float(closes[-1])
                prev_close = float(closes[-2])
                today_open = float(opens[-1]) if not np.isnan(opens[-1]) else current

                if prev_close <= 0 or current <= 0:
                    continue

                change_pct = round((current - prev_close) / prev_close * 100, 2)
                gap_pct    = round((today_open - prev_close) / prev_close * 100, 2)

                # Hacim
                today_vol    = float(volumes[-1]) if not np.isnan(volumes[-1]) else 0
                vol_window   = volumes[-20:] if len(volumes) >= 20 else volumes
                vol_window   = vol_window[~np.isnan(vol_window)]
                avg_vol_20   = float(vol_window.mean()) if len(vol_window) else 0
                vol_ratio    = round(today_vol / avg_vol_20, 2) if avg_vol_20 > 0 else 0

                # Likidite (TRY cinsinden ortalama günlük hacim)
                avg_vol_try  = avg_vol_20 * current

                # Basit momentum (Evre 1 — çarpraz doğrulama olmadan)
                basic_score = _basic_momentum(change_pct, gap_pct, vol_ratio)

                stage1_metrics[ticker] = {
                    "price":          round(current, 2),
                    "prev_close":     round(prev_close, 2),
                    "today_open":     round(today_open, 2),
                    "change_pct":     change_pct,
                    "gap_pct":        gap_pct,
                    "volume":         int(today_vol),
                    "avg_volume_20d": int(avg_vol_20),
                    "volume_ratio":   vol_ratio,
                    "avg_vol_try":    avg_vol_try,
                    "basic_momentum": basic_score,
                    "_closes":        closes,
                    "_opens":         opens,
                    "_highs":         t_df["High"].values.astype(float),
                    "_lows":          t_df["Low"].values.astype(float),
                    "_volumes":       volumes,
                }

                if ticker == BENCHMARK:
                    benchmark_change = change_pct
                    benchmark_price  = current
            except Exception:
                continue

        # ─── EVRE 1 → EVRE 2: Pre-filter ──────────────────────────
        candidates: list = []
        for ticker, m in stage1_metrics.items():
            if ticker == BENCHMARK:
                continue  # benchmark sadece rejim için, analiz etme

            # Likidite tabanı
            if m["avg_vol_try"] < MIN_LIQUIDITY_TRY:
                continue

            # En az bir momentum kriteri
            passes = (
                m["volume_ratio"]  >= PREFILTER_MIN_VOLUME_RATIO
                or m["basic_momentum"] >= PREFILTER_MIN_MOMENTUM_SCORE
                or abs(m["change_pct"]) >= PREFILTER_MIN_ABS_CHANGE
            )
            if passes:
                candidates.append((ticker, m["basic_momentum"]))

        # Top N adayı seç
        candidates.sort(key=lambda x: x[1], reverse=True)
        top_candidates = [t for t, _ in candidates[:PREFILTER_TOP_N]]

        # Evre 2 evreni
        if SCAN_UNIVERSE == "watchlist":
            stage2_tickers = list(focus_list)
        elif SCAN_UNIVERSE == "full":
            stage2_tickers = top_candidates
        else:  # hybrid (default)
            stage2_tickers = list(set(focus_list) | set(top_candidates))

        # ─── EVRE 2: Derin analiz ─────────────────────────────────
        result = {}
        for ticker in stage2_tickers:
            m = stage1_metrics.get(ticker)
            if not m:
                continue
            try:
                closes  = m["_closes"]
                highs   = m["_highs"]
                lows    = m["_lows"]
                volumes = m["_volumes"]
                current = m["price"]

                # NaN temizle
                import numpy as np
                if np.isnan(closes).any():
                    # Son NaN olmayan değerlerle forward-fill
                    mask = ~np.isnan(closes)
                    if not mask.any():
                        continue
                    closes = closes[mask]
                    highs  = highs[mask]
                    lows   = lows[mask]
                    volumes = volumes[mask]

                closes_list  = closes.tolist()
                highs_list   = highs.tolist()
                lows_list    = lows.tolist()
                volumes_list = volumes.tolist()

                ema9  = _ema(closes_list, 9)
                ema21 = _ema(closes_list, 21)
                ema50 = _ema(closes_list, 50)
                rsi14 = _rsi(closes_list, 14)
                atr14 = _atr(highs_list, lows_list, closes_list, 14)
                atr_pct = round(atr14 / current * 100, 2) if current > 0 else 0
                vwap = _vwap_approx(highs_list[-5:], lows_list[-5:], closes_list[-5:], volumes_list[-5:])
                macd_data = _macd(closes_list)
                bb_data = _bollinger_bands(closes_list)
                trend = _detect_trend(closes_list, ema9, ema21, ema50)

                signal = _generate_signal(
                    ema9, ema21, ema50, rsi14, m["volume_ratio"], m["change_pct"],
                    m["gap_pct"], trend,
                    macd_data=macd_data, bb_data=bb_data, current_price=current,
                )
                momentum_score = _calc_momentum_score(
                    m["change_pct"], m["gap_pct"], m["volume_ratio"], rsi14,
                    ema9, ema21, ema50, atr_pct, trend,
                    macd_data=macd_data, bb_data=bb_data, current_price=current,
                )

                today_high = float(highs[-1]) if len(highs) else current
                today_low  = float(lows[-1])  if len(lows)  else current

                result[ticker] = {
                    "price":          current,
                    "open":           m["today_open"],
                    "high":           round(today_high, 2),
                    "low":            round(today_low, 2),
                    "prev_close":     m["prev_close"],
                    "change_pct":     m["change_pct"],
                    "gap_pct":        m["gap_pct"],
                    "volume":         m["volume"],
                    "avg_volume_20d": m["avg_volume_20d"],
                    "avg_vol_try":    round(m["avg_vol_try"], 0),
                    "volume_ratio":   m["volume_ratio"],
                    "ema9":           round(ema9, 2),
                    "ema21":          round(ema21, 2),
                    "ema50":          round(ema50, 2),
                    "rsi14":          round(rsi14, 1),
                    "atr14":          round(atr14, 2),
                    "atr_pct":        atr_pct,
                    "vwap":           round(vwap, 2),
                    "momentum_score": momentum_score,
                    "signal":         signal,
                    "trend":          trend,
                    "macd":           macd_data["macd"],
                    "macd_signal":    macd_data["signal"],
                    "macd_histogram": macd_data["histogram"],
                    "macd_cross":     macd_data["cross"],
                    "bb_upper":       bb_data["upper"],
                    "bb_middle":      bb_data["middle"],
                    "bb_lower":       bb_data["lower"],
                    "bb_width":       bb_data["width"],
                    "bb_position":    bb_data["position"],
                    "bars_5d":        [round(c, 2) for c in closes_list[-5:]],
                    "relative_strength": round(m["change_pct"] - benchmark_change, 2),
                    "in_focus":       ticker in focus_list,
                }
            except Exception:
                continue

        # Meta
        market_open = is_market_open()
        regime = _detect_regime(result, benchmark_change)

        result["_meta"] = {
            "market_open":      market_open,
            "scan_time":        datetime.now(timezone.utc).isoformat(),
            "regime":           regime,
            "benchmark":        BENCHMARK,
            "benchmark_change": benchmark_change,
            "total_scanned":    len(stage1_metrics),
            "universe_size":    len(universe),
            "stage1_passed":    len(candidates),
            "stage2_analyzed":  len([k for k in result if not k.startswith("_")]),
            "top_candidates":   [t for t, _ in candidates[:10]],
            "bullish_count":    len([k for k, v in result.items()
                                     if isinstance(v, dict) and v.get("signal") == "strong_buy"]),
            "scan_universe":    SCAN_UNIVERSE,
        }
        return result

    except Exception as e:
        return {"error": str(e)}


# ─── Basit momentum skoru (Evre 1 için hızlı) ─────────────────────

def _basic_momentum(change_pct: float, gap_pct: float, vol_ratio: float) -> float:
    """
    0-100 arası basit skor — tam indikatör hesaplamadan önceki kaba elek.
    """
    score = 50
    score += min(max(change_pct * 3, -20), 20)      # ±20
    score += min(max(gap_pct * 2, -10), 10)         # ±10
    if vol_ratio >= 3.0:
        score += 20
    elif vol_ratio >= 2.0:
        score += 12
    elif vol_ratio >= 1.3:
        score += 6
    elif vol_ratio < 0.5:
        score -= 10
    return round(max(0, min(100, score)), 1)


# ─── Tam İndikatör Hesaplamaları (Evre 2) ────────────────────────

def _ema(prices: list, period: int) -> float:
    if len(prices) < period:
        return sum(prices) / len(prices) if prices else 0
    k = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = price * k + ema * (1 - k)
    return ema


def _rsi(prices: list, period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50.0
    deltas = [prices[i] - prices[i-1] for i in range(1, len(prices))]
    gains  = [max(d, 0) for d in deltas[:period]]
    losses = [max(-d, 0) for d in deltas[:period]]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for d in deltas[period:]:
        avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
        avg_loss = (avg_loss * (period - 1) + max(-d, 0)) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 1)


def _atr(highs: list, lows: list, closes: list, period: int = 14) -> float:
    if len(closes) < 2:
        return 0.0
    trs = []
    for i in range(1, len(closes)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i-1]),
            abs(lows[i] - closes[i-1]),
        )
        trs.append(tr)
    if not trs:
        return 0.0
    atr = sum(trs[:period]) / min(len(trs), period)
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def _macd(closes: list, fast: int = 12, slow: int = 26, signal_period: int = 9) -> dict:
    if len(closes) < slow + signal_period:
        return {"macd": 0, "signal": 0, "histogram": 0, "cross": "none"}
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    macd_line = round(ema_fast - ema_slow, 4)
    macd_series = []
    k_fast = 2 / (fast + 1)
    k_slow = 2 / (slow + 1)
    ef = sum(closes[:fast]) / fast
    es = sum(closes[:slow]) / slow
    for i in range(slow, len(closes)):
        ef = closes[i] * k_fast + ef * (1 - k_fast)
        es = closes[i] * k_slow + es * (1 - k_slow)
        macd_series.append(ef - es)
    if len(macd_series) >= signal_period:
        k_sig = 2 / (signal_period + 1)
        sig = sum(macd_series[:signal_period]) / signal_period
        for val in macd_series[signal_period:]:
            sig = val * k_sig + sig * (1 - k_sig)
        signal_line = round(sig, 4)
    else:
        signal_line = 0
    histogram = round(macd_line - signal_line, 4)
    cross = "none"
    if len(macd_series) >= 2:
        prev_hist = macd_series[-2] - signal_line
        if histogram > 0 and prev_hist <= 0:
            cross = "bullish_cross"
        elif histogram < 0 and prev_hist >= 0:
            cross = "bearish_cross"
    return {
        "macd": round(macd_line, 2),
        "signal": round(signal_line, 2),
        "histogram": round(histogram, 2),
        "cross": cross,
    }


def _bollinger_bands(closes: list, period: int = 20, std_dev: float = 2.0) -> dict:
    if len(closes) < period:
        price = closes[-1] if closes else 0
        return {"upper": price, "middle": price, "lower": price, "width": 0, "position": 0.5}
    window = closes[-period:]
    middle = sum(window) / period
    variance = sum((x - middle) ** 2 for x in window) / period
    std = math.sqrt(variance)
    upper = round(middle + std_dev * std, 2)
    lower = round(middle - std_dev * std, 2)
    middle = round(middle, 2)
    width = round((upper - lower) / middle * 100, 2) if middle > 0 else 0
    current = closes[-1]
    band_range = upper - lower
    position = round((current - lower) / band_range, 2) if band_range > 0 else 0.5
    return {
        "upper": upper, "middle": middle, "lower": lower,
        "width": width, "position": max(0, min(1, position)),
    }


def _vwap_approx(highs: list, lows: list, closes: list, volumes: list) -> float:
    if not volumes or sum(volumes) == 0:
        return closes[-1] if closes else 0
    total_pv = sum(
        ((h + l + c) / 3) * v
        for h, l, c, v in zip(highs, lows, closes, volumes)
    )
    return total_pv / sum(volumes)


def _detect_trend(closes: list, ema9: float, ema21: float, ema50: float) -> str:
    price = closes[-1]
    if price > ema9 > ema21 > ema50:
        return "strong_uptrend"
    elif price > ema21 and ema9 > ema21:
        return "uptrend"
    elif price < ema9 < ema21 < ema50:
        return "strong_downtrend"
    elif price < ema21 and ema9 < ema21:
        return "downtrend"
    return "sideways"


def _generate_signal(ema9, ema21, ema50, rsi, vol_ratio, change_pct, gap_pct, trend,
                     macd_data=None, bb_data=None, current_price=0):
    """
    V3 Confluence sinyal üretimi — Ross Cameron + MACD + Bollinger.
    Sinyal: strong_buy / buy / neutral / sell / strong_sell
    """
    score = 0
    if trend == "strong_uptrend":
        score += 3
    elif trend == "uptrend":
        score += 2
    elif trend == "downtrend":
        score -= 2
    elif trend == "strong_downtrend":
        score -= 3

    if 40 <= rsi <= 65:
        score += 2
    elif 30 <= rsi < 40:
        score += 1
    elif rsi > 80:
        score -= 2
    elif rsi < 25:
        score += 1

    if vol_ratio >= SIGNAL_VOLUME_THRESHOLD:
        score += 3
    elif vol_ratio >= 1.3:
        score += 1
    elif vol_ratio < 0.5:
        score -= 1

    if gap_pct >= SIGNAL_GAP_THRESHOLD and vol_ratio >= 1.5:
        score += 3
    elif gap_pct >= 2.0 and vol_ratio >= 1.2:
        score += 2
    elif gap_pct <= -SIGNAL_GAP_THRESHOLD:
        score -= 2

    if change_pct >= 3.0:
        score += 1
    elif change_pct <= -3.0:
        score -= 1

    if macd_data:
        if macd_data["cross"] == "bullish_cross":
            score += 2
        elif macd_data["cross"] == "bearish_cross":
            score -= 2
        elif macd_data["histogram"] > 0:
            score += 1
        elif macd_data["histogram"] < 0:
            score -= 1

    if bb_data and current_price > 0:
        bb_pos = bb_data["position"]
        if bb_pos <= 0.1:
            score += 2
        elif bb_pos >= 0.9:
            score -= 1
        if bb_data["width"] > 8:
            score += 1

    if score >= 7:
        return "strong_buy"
    elif score >= 3:
        return "buy"
    elif score <= -5:
        return "strong_sell"
    elif score <= -2:
        return "sell"
    return "neutral"


def _calc_momentum_score(change_pct, gap_pct, vol_ratio, rsi, ema9, ema21, ema50,
                         atr_pct, trend, macd_data=None, bb_data=None, current_price=0):
    """V3 Bileşik momentum skoru (0-100)."""
    score = 50
    score += min(max(change_pct * 3, -20), 20)
    score += min(max(gap_pct * 2, -10), 10)
    if vol_ratio >= 3.0:
        score += 15
    elif vol_ratio >= 2.0:
        score += 10
    elif vol_ratio >= 1.3:
        score += 5
    elif vol_ratio < 0.5:
        score -= 10

    trend_map = {
        "strong_uptrend": 15, "uptrend": 8, "sideways": 0,
        "downtrend": -8, "strong_downtrend": -15,
    }
    score += trend_map.get(trend, 0)

    if 1.5 <= atr_pct <= 4.0:
        score += 5
    elif atr_pct > 6.0:
        score -= 5

    if rsi > 80:
        score -= 10
    elif rsi < 20:
        score -= 5

    if macd_data:
        if macd_data["cross"] == "bullish_cross":
            score += 8
        elif macd_data["cross"] == "bearish_cross":
            score -= 8
        elif macd_data["histogram"] > 0:
            score += 3
        elif macd_data["histogram"] < 0:
            score -= 3

    if bb_data:
        bb_pos = bb_data["position"]
        if bb_pos <= 0.15:
            score += 5
        elif bb_pos >= 0.85:
            score -= 3

    return max(0, min(100, round(score)))


def _detect_regime(market_data: dict, benchmark_change: float = 0.0) -> str:
    """
    BIST piyasa rejimi — XU100 + genel momentum skorlarına göre.
    Çıktı: bull_strong / bull / neutral / bear / bear_strong / unknown
    """
    scores = [
        v.get("momentum_score", 50)
        for k, v in market_data.items()
        if isinstance(v, dict) and not k.startswith("_") and "momentum_score" in v
    ]
    avg_momentum = sum(scores) / len(scores) if scores else 50

    if benchmark_change > 1.5 and avg_momentum > 65:
        return "bull_strong"
    elif benchmark_change > 0.5 and avg_momentum > 55:
        return "bull"
    elif benchmark_change < -1.5 and avg_momentum < 35:
        return "bear_strong"
    elif benchmark_change < -0.5 and avg_momentum < 45:
        return "bear"
    return "neutral"


# ─── Market Saatleri (BIST) ──────────────────────────────────────

def is_market_open() -> bool:
    """BIST: Pzt-Cum, 10:00-18:00 Istanbul (TRT = UTC+3)."""
    now_utc = datetime.now(timezone.utc)
    now_tr = now_utc + timedelta(hours=3)
    if now_tr.weekday() >= 5:
        return False
    return 10 <= now_tr.hour < 18


def is_premarket() -> bool:
    """BIST'te pre-market yok — her zaman False."""
    return False


# ─── Multi-Timeframe Analiz (opsiyonel — Evre 2 adaylarına uygula) ─

def get_multi_timeframe(tickers: list = None) -> dict:
    """
    Birden fazla zaman diliminde teknik analiz.
    1h / 4h / 1d mumlar ile timeframe confluence.
    """
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np

        symbols = tickers or get_focus_list()
        end = datetime.now(timezone.utc)
        result = {}

        timeframes = [
            ("1h", "1h",  timedelta(days=30)),
            ("4h", "1h",  timedelta(days=60)),   # yfinance 4h native yok → 1h'den türet
            ("1d", "1d",  timedelta(days=LOOKBACK_DAYS)),
        ]

        for tf_label, yf_interval, lookback in timeframes:
            try:
                df = yf.download(
                    tickers=" ".join(symbols),
                    start=(end - lookback).strftime("%Y-%m-%d"),
                    end=end.strftime("%Y-%m-%d"),
                    interval=yf_interval,
                    group_by="ticker",
                    threads=True, progress=False, auto_adjust=True,
                )
                for ticker in symbols:
                    try:
                        if isinstance(df.columns, pd.MultiIndex):
                            if ticker not in df.columns.get_level_values(0):
                                continue
                            t_df = df[ticker].dropna(how="all")
                        else:
                            t_df = df.dropna(how="all")
                        if len(t_df) < 5:
                            continue

                        # 4h için: 1h barları grupla
                        if tf_label == "4h":
                            t_df = t_df.resample("4H").agg({
                                "Open": "first", "High": "max",
                                "Low": "min", "Close": "last", "Volume": "sum"
                            }).dropna()
                            if len(t_df) < 5:
                                continue

                        closes = t_df["Close"].values.astype(float).tolist()
                        ema9 = _ema(closes, 9)
                        ema21 = _ema(closes, 21)
                        ema50 = _ema(closes, min(50, len(closes)))
                        rsi14 = _rsi(closes, 14)
                        macd_data = _macd(closes)
                        trend = _detect_trend(closes, ema9, ema21, ema50)

                        if ticker not in result:
                            result[ticker] = {}
                        result[ticker][tf_label] = {
                            "ema9": round(ema9, 2), "ema21": round(ema21, 2),
                            "rsi14": round(rsi14, 1), "trend": trend,
                            "macd": macd_data["macd"],
                            "macd_histogram": macd_data["histogram"],
                            "macd_cross": macd_data["cross"],
                            "price": round(closes[-1], 2),
                        }
                    except Exception:
                        continue
            except Exception:
                continue

        for ticker, tf_data in result.items():
            result[ticker]["confluence"] = _calc_confluence(tf_data)
        return result
    except Exception as e:
        return {"error": str(e)}


def _calc_confluence(tf_data: dict) -> dict:
    trend_scores = {"strong_uptrend": 2, "uptrend": 1, "sideways": 0,
                    "downtrend": -1, "strong_downtrend": -2}
    total = 0
    count = 0
    for tf in ("1h", "4h", "1d"):
        if tf in tf_data:
            trend = tf_data[tf].get("trend", "sideways")
            total += trend_scores.get(trend, 0)
            hist = tf_data[tf].get("macd_histogram", 0)
            if hist > 0:
                total += 0.5
            elif hist < 0:
                total -= 0.5
            count += 1
    if count == 0:
        return {"direction": "unknown", "score": 50, "alignment": 0}
    avg = total / count
    score = round(max(0, min(100, (avg + 3) / 6 * 100)))
    if avg >= 2.0:
        direction = "strong_bullish"
    elif avg >= 1.0:
        direction = "bullish"
    elif avg <= -2.0:
        direction = "strong_bearish"
    elif avg <= -1.0:
        direction = "bearish"
    else:
        direction = "mixed"
    return {"direction": direction, "score": score, "alignment": round(avg, 1)}


# ─── Korelasyon Matrisi (sadece Focus List) ──────────────────────

def get_correlation_matrix(tickers: list = None) -> dict:
    """
    Hisseler arası Pearson korelasyon matrisi (günlük getiriler).
    Varsayılan: Focus List (100 ticker'lık matris çok büyük olur).
    """
    try:
        import yfinance as yf
        import pandas as pd
        import numpy as np

        symbols = tickers or get_focus_list()
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=60)

        df = yf.download(
            tickers=" ".join(symbols),
            start=start.strftime("%Y-%m-%d"),
            end=end.strftime("%Y-%m-%d"),
            interval="1d",
            group_by="ticker", threads=True,
            progress=False, auto_adjust=True,
        )

        returns = {}
        for ticker in symbols:
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if ticker not in df.columns.get_level_values(0):
                        continue
                    closes = df[ticker]["Close"].dropna().values.astype(float)
                else:
                    closes = df["Close"].dropna().values.astype(float)
                if len(closes) < 10:
                    continue
                daily_returns = [(closes[i] - closes[i-1]) / closes[i-1]
                                 for i in range(1, len(closes))]
                returns[ticker] = daily_returns
            except Exception:
                continue

        tickers_with_data = list(returns.keys())
        matrix = {}
        high_corrs = []
        for i, t1 in enumerate(tickers_with_data):
            matrix[t1] = {}
            for j, t2 in enumerate(tickers_with_data):
                if i == j:
                    matrix[t1][t2] = 1.0
                    continue
                corr = _pearson_correlation(returns[t1], returns[t2])
                matrix[t1][t2] = corr
                if i < j and abs(corr) >= 0.7:
                    high_corrs.append({"pair": f"{t1}-{t2}", "corr": corr})
        high_corrs.sort(key=lambda x: abs(x["corr"]), reverse=True)

        if len(high_corrs) > 0:
            avg_high = sum(abs(c["corr"]) for c in high_corrs) / len(high_corrs)
            div_score = round(max(0, (1 - avg_high) * 100))
        else:
            div_score = 90

        return {
            "matrix": matrix,
            "high_correlations": high_corrs[:10],
            "diversification_score": div_score,
            "tickers": tickers_with_data,
        }
    except Exception as e:
        return {"error": str(e), "matrix": {}, "high_correlations": [], "diversification_score": 0}


def _pearson_correlation(x: list, y: list) -> float:
    n = min(len(x), len(y))
    if n < 5:
        return 0.0
    x = x[-n:]
    y = y[-n:]
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    num = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, y))
    den_x = math.sqrt(sum((xi - mean_x) ** 2 for xi in x))
    den_y = math.sqrt(sum((yi - mean_y) ** 2 for yi in y))
    if den_x == 0 or den_y == 0:
        return 0.0
    return round(num / (den_x * den_y), 2)
