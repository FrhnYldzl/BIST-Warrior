"""
portfolio_agent.py — Portföy Yönetim Ajanı (Sprint 1B)

Açık pozisyonları aktif olarak yönetir:
  • review_positions(positions, market_data) → her pozisyon için hold/
    reduce/close önerisi + gerekçe
  • correlation_warnings(positions) → aynı yöne giden / sektör çakışan
    pozisyonlar için uyarı
  • kelly_size(win_rate, avg_w, avg_l, equity) → Kelly Criterion ile
    optimum pozisyon büyüklüğü (yarım Kelly güvenliği için)
  • day_quality(stats) → gün kalite skoru: işlem hızı, isabet oranı,
    PnL momentum

Çıktı endpoint'leri AI Council ve Aktif Pozisyonlar view'larında kullanılır.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

try:
    from config import SECTOR_MAP, MAX_SECTOR_PCT
except Exception:
    SECTOR_MAP, MAX_SECTOR_PCT = {}, 0.5


# ──────────────────────────────────────────────────────────────────
# Yardımcılar
# ──────────────────────────────────────────────────────────────────

def _to_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        if isinstance(v, str):
            v = v.replace(",", ".").replace("₺", "").strip()
        return float(v)
    except Exception:
        return default


def _ticker_sector(ticker: str) -> str:
    if not ticker:
        return "Diger"
    base = ticker.replace(".IS", "").upper()
    if isinstance(SECTOR_MAP, dict):
        return SECTOR_MAP.get(base, SECTOR_MAP.get(ticker, "Diger"))
    return "Diger"


def _minutes_since(iso_ts: str | None) -> int:
    if not iso_ts:
        return 0
    try:
        dt = datetime.fromisoformat(str(iso_ts).replace("Z", "+00:00"))
        return int((datetime.now(timezone.utc) - dt).total_seconds() // 60)
    except Exception:
        return 0


# ──────────────────────────────────────────────────────────────────
# Pozisyon İncelemesi
# ──────────────────────────────────────────────────────────────────

def review_positions(positions: list, market_data: dict | None = None) -> list[dict]:
    """
    Her açık pozisyon için hold/reduce/close önerisi üret.

    Inputs:
      positions: [{ticker, qty, entry_price, stop_loss, take_profit, opened_at, current_price, max_hold_minutes}]
      market_data: optional {ticker: {price, rsi14, macd_cross, change_pct, ...}}

    Returns: [{ticker, recommendation: hold|reduce|close, reasons: [...], urgency, current_pnl_pct, ...}]
    """
    out = []
    md = market_data or {}

    for p in positions or []:
        ticker = p.get("ticker") or ""
        qty = _to_float(p.get("qty"))
        entry = _to_float(p.get("entry_price") or p.get("avg_entry") or p.get("avg_entry_price"))
        sl = _to_float(p.get("stop_loss"))
        tp = _to_float(p.get("take_profit"))
        max_hold = int(p.get("max_hold_minutes") or 0)
        opened_at = p.get("opened_at") or p.get("entry_time") or p.get("created_at")

        # Mevcut fiyat: önce market_data, yoksa pozisyon current_price
        market = md.get(ticker) or {}
        current = _to_float(market.get("price")) or _to_float(p.get("current_price"))

        if entry <= 0 or current <= 0 or qty <= 0:
            continue

        pnl_pct = (current - entry) / entry * 100
        held_min = _minutes_since(opened_at)

        reasons: list[str] = []
        rec = "hold"
        urgency = "low"

        # ─── Çıkış sinyalleri ─────────────────────────────────────
        # 1) TP'ye yakın (<%1) — kısmi kâr al
        if tp > 0 and current >= tp * 0.99:
            rec = "reduce"
            urgency = "high"
            reasons.append(f"Hedef ₺{tp:.2f}'a %{((tp-current)/current*100):.2f} kaldı — kısmi kâr al")

        # 2) SL'e yakın (<%0.8)
        if sl > 0 and current <= sl * 1.008:
            rec = "close"
            urgency = "critical"
            reasons.append(f"Stop ₺{sl:.2f}'a %{((current-sl)/current*100):.2f} kaldı — kapatmayı düşün")

        # 3) RSI aşırı alım — mean reversion
        rsi = _to_float(market.get("rsi14"))
        if rsi >= 78:
            if rec == "hold":
                rec = "reduce"
                urgency = "medium"
            reasons.append(f"RSI {rsi:.0f} aşırı alım — kısmi çıkış değerlendir")
        elif rsi <= 28 and pnl_pct > 0:
            reasons.append(f"RSI {rsi:.0f} dipte ama pozitif P&L — momentum tükenmiş olabilir")

        # 4) MACD bearish cross (long pozisyondaysa)
        macd_cross = (market.get("macd_cross") or "").lower()
        if "bearish" in macd_cross:
            if rec == "hold":
                rec = "reduce"
                urgency = "medium"
            reasons.append("MACD bearish cross — momentum dönüyor")

        # 5) max_hold süresi geçti
        if max_hold > 0 and held_min >= max_hold:
            rec = "close"
            urgency = "high"
            reasons.append(f"Hold süresi {held_min}/{max_hold} dk doldu — kapatmak gerek")
        elif max_hold > 0 and held_min >= max_hold * 0.8:
            if rec == "hold":
                urgency = "medium"
            reasons.append(f"Hold süresi {held_min}/{max_hold} dk — son %20 dilim, çıkış hazırlığı")

        # 6) EOD (TR seansı 18:00 — 17:55'de uyar)
        now_tr = datetime.now(timezone.utc) + timedelta(hours=3)
        if now_tr.hour == 17 and now_tr.minute >= 55:
            rec = "close"
            urgency = "critical"
            reasons.append("Seans bitiyor — gün-içi pozisyon kapanmalı (EOD)")

        # 7) Olağan hold — sadece "kâr/zarar" durumu
        if not reasons:
            if pnl_pct > 0:
                reasons.append(f"+%{pnl_pct:.2f} kârda — TP'ye doğru izle")
            else:
                reasons.append(f"%{pnl_pct:.2f} — hold, SL korumada")

        out.append({
            "ticker": ticker,
            "recommendation": rec,
            "urgency": urgency,
            "reasons": reasons,
            "current_pnl_pct": round(pnl_pct, 2),
            "entry_price": entry,
            "current_price": current,
            "stop_loss": sl,
            "take_profit": tp,
            "qty": qty,
            "held_minutes": held_min,
            "max_hold_minutes": max_hold,
            "rsi": rsi or None,
        })

    return out


# ──────────────────────────────────────────────────────────────────
# Korelasyon / yoğunluk uyarıları
# ──────────────────────────────────────────────────────────────────

def correlation_warnings(positions: list) -> list[dict]:
    """Aynı sektör veya yön çakışması olan pozisyonları belirle."""
    if not positions:
        return []

    by_sector: dict[str, list[str]] = {}
    for p in positions:
        sec = _ticker_sector(p.get("ticker", ""))
        by_sector.setdefault(sec, []).append(p.get("ticker", ""))

    warnings = []
    for sec, tickers in by_sector.items():
        if len(tickers) >= 3:
            warnings.append({
                "type": "sector_concentration",
                "severity": "high",
                "sector": sec,
                "tickers": tickers,
                "message": f"{sec} sektöründe {len(tickers)} pozisyon — sektörel risk yoğunlaşması",
            })
        elif len(tickers) == 2:
            warnings.append({
                "type": "sector_concentration",
                "severity": "medium",
                "sector": sec,
                "tickers": tickers,
                "message": f"{sec} sektöründe 2 pozisyon — yeni eklemede dikkat",
            })

    return warnings


# ──────────────────────────────────────────────────────────────────
# Kelly Criterion — optimum pozisyon büyüklüğü
# ──────────────────────────────────────────────────────────────────

def kelly_fraction(win_rate: float, avg_winner_pct: float, avg_loser_pct: float) -> float:
    """
    Kelly fraction. b = avg_winner / avg_loser, p = win_rate.
    Kelly = (p*b - (1-p)) / b
    Yarım Kelly daha güvenli (volatilite azaltma). Negatifse 0 döner.
    """
    p = max(0.0, min(1.0, win_rate / 100 if win_rate > 1 else win_rate))
    avg_w = abs(avg_winner_pct or 0)
    avg_l = abs(avg_loser_pct or 0)
    if avg_l == 0 or avg_w == 0 or p == 0:
        return 0.0
    b = avg_w / avg_l
    full_kelly = (p * b - (1 - p)) / b
    half_kelly = max(0.0, full_kelly / 2)  # yarım Kelly
    return min(half_kelly, 0.25)  # max %25 cap (deli risk olmasın)


def kelly_size(stats: dict, equity: float) -> dict:
    """
    Tarihsel sinyal performansından Kelly büyüklüğü öner.

    Args:
        stats: signal_history.get_performance() çıktısı
        equity: portföy equity'si (TRY)

    Returns:
        {kelly_pct, kelly_try, basis: {win_rate, avg_w, avg_l}, recommendation}
    """
    win_rate = _to_float(stats.get("hit_rate_pct", 0))
    avg_w = _to_float(stats.get("avg_winner_pct", 0))
    avg_l = _to_float(stats.get("avg_loser_pct", 0))
    total = int(stats.get("total_signals", 0) or 0)

    fraction = kelly_fraction(win_rate, avg_w, avg_l)
    kelly_try = fraction * equity

    if total < 20:
        rec = "Yetersiz veri (<20 sinyal) — Kelly güvenilmez, sabit %1.5-2 kullan"
        confidence = "low"
    elif fraction <= 0:
        rec = "Negatif edge — Kelly 0; strateji kâr getirmiyor"
        confidence = "high"
    elif fraction < 0.05:
        rec = f"Zayıf edge — yarım Kelly %{fraction*100:.1f}, max %2 öneri"
        confidence = "medium"
    elif fraction < 0.15:
        rec = f"Sağlıklı edge — yarım Kelly %{fraction*100:.1f} = ₺{kelly_try:,.0f}"
        confidence = "high"
    else:
        rec = f"Yüksek edge — yarım Kelly %{fraction*100:.1f}; volatilite için %25 cap'lendi"
        confidence = "high"

    return {
        "kelly_pct": round(fraction * 100, 2),
        "kelly_try": round(kelly_try, 2),
        "confidence": confidence,
        "recommendation": rec,
        "basis": {
            "win_rate_pct": win_rate,
            "avg_winner_pct": avg_w,
            "avg_loser_pct": avg_l,
            "total_signals": total,
        },
    }


# ──────────────────────────────────────────────────────────────────
# Gün kalitesi
# ──────────────────────────────────────────────────────────────────

def day_quality(today_stats: dict) -> dict:
    """Bugünün performansından gün kalite skoru."""
    n = int(today_stats.get("n_trades_today", 0) or 0)
    pnl_pct = _to_float(today_stats.get("realized_pnl_pct", 0))
    win_rate = _to_float(today_stats.get("win_rate_pct", 0))
    goal_reached = bool(today_stats.get("goal_reached", False))
    stop_triggered = bool(today_stats.get("stop_triggered", False))

    score = 50  # baseline
    label = "neutral"
    notes: list[str] = []

    if stop_triggered:
        score -= 40
        label = "stopped"
        notes.append("Günlük max zarar tetiklendi — yeni pozisyon yok")
    elif goal_reached:
        score += 30
        label = "goal_reached"
        notes.append("Günlük hedef gerçekleşti — defansif moda geç")
    elif n == 0:
        score = 50
        label = "no_trades"
        notes.append("Henüz işlem yok")
    elif pnl_pct > 0 and win_rate >= 60:
        score += 25
        label = "strong"
        notes.append(f"Pozitif P&L %{pnl_pct:.2f}, isabet %{win_rate:.0f} — sürdür")
    elif pnl_pct < 0 and win_rate < 40:
        score -= 25
        label = "weak"
        notes.append(f"Negatif P&L %{pnl_pct:.2f}, isabet %{win_rate:.0f} — yavaşla, daha seçici ol")
    else:
        notes.append(f"Karışık: %{pnl_pct:.2f} P&L, %{win_rate:.0f} isabet")

    score = max(0, min(100, score))
    return {"score": score, "label": label, "notes": notes}
