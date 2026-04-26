"""
risk_agent.py — Risk Agent (Sprint 1A)

Mevcut risk_manager.py ham hesaplamaları yapıyor (pozisyon boyutlandırma,
ATR stop, Sharpe/VaR). Risk Agent bunun ÜSTÜNDE konuşur:

  • assess_signal(signal, portfolio) → bu sinyal portföye eklendiğinde
    risk skoru ne, hangi uyarılar var, önerilen TRY ve adet ne?
  • assess_portfolio(positions, equity, planned_signals) → portföyün şu
    anki + planlananla birleşik risk durumu (sektör, korelasyon, VaR)
  • simulate_signal_addition(signal, portfolio) → sinyali eklemeden ÖNCE
    "ne olur" gösterimi (delta dashboard için)

Çıktı `verdict`: GREEN / YELLOW / RED — Council kararına girer.
"""

from __future__ import annotations

from typing import Optional

from risk_manager import RiskManager
from config import (
    MAX_POSITION_PCT, MAX_SECTOR_PCT, MAX_RISK_PCT,
    REGIME_MAX_INVESTED, SECTOR_MAP,
)


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


def _entry_midpoint(signal: dict) -> float:
    """entry_zone "210.50-212.00" → 211.25; tek değer → o değer."""
    z = signal.get("entry_zone") or signal.get("entry") or signal.get("entry_price")
    if not z:
        return _to_float(signal.get("price"))
    if isinstance(z, (int, float)):
        return float(z)
    s = str(z).replace(",", ".").replace("₺", "").strip()
    import re
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        return 0.0
    if len(nums) == 1:
        return float(nums[0])
    return (float(nums[0]) + float(nums[1])) / 2


def _ticker_sector(ticker: str) -> str:
    """SECTOR_MAP'ten sektör. Bulamazsa "Diger"."""
    if not ticker:
        return "Diger"
    base = ticker.replace(".IS", "").upper()
    if isinstance(SECTOR_MAP, dict):
        return SECTOR_MAP.get(base, SECTOR_MAP.get(ticker, "Diger"))
    return "Diger"


# ──────────────────────────────────────────────────────────────────
# Sinyal seviyesi risk değerlendirmesi
# ──────────────────────────────────────────────────────────────────

def assess_signal(
    signal: dict,
    portfolio: dict,
    regime: str = "neutral",
) -> dict:
    """
    Tek sinyal için risk analizi.

    Returns:
        {
          "verdict": "GREEN | YELLOW | RED",
          "risk_score": 0-100,                # düşük = az riskli
          "warnings": [...],
          "recommended_size_try": float,
          "recommended_qty": int,
          "stop_distance_pct": float,
          "reward_distance_pct": float,
          "rr_ratio": float,
          "sector": str,
          "sector_after_pct": float,          # bu sinyal eklenirse sektör yoğunluğu
          "position_after_pct": float,        # bu pozisyon portföyün %?
          "risk_amount_try": float,           # entry-stop arası TRY zarar potansiyeli
          "risk_pct_of_equity": float,        # zarar potansiyelinin equity'ye oranı
        }
    """
    equity = _to_float(portfolio.get("equity") or portfolio.get("cash") or 0)
    cash = _to_float(portfolio.get("cash") or 0)
    positions = portfolio.get("positions") or []

    ticker = signal.get("ticker") or signal.get("symbol") or ""
    confidence = int(signal.get("confidence") or 0)
    entry = _entry_midpoint(signal)
    sl = _to_float(signal.get("stop_loss"))
    tp = _to_float(signal.get("take_profit"))

    warnings: list[str] = []

    # Temel mantık
    if equity <= 0:
        return {
            "verdict": "RED",
            "risk_score": 100,
            "warnings": ["Equity sıfır — risk hesaplanamadı"],
            "recommended_size_try": 0,
            "recommended_qty": 0,
        }

    if entry <= 0 or sl <= 0:
        return {
            "verdict": "RED",
            "risk_score": 95,
            "warnings": ["Geçersiz entry/SL fiyatı"],
            "recommended_size_try": 0,
            "recommended_qty": 0,
        }

    if sl >= entry:
        warnings.append("Stop-loss giriş üstünde — long sinyal için anlamsız")

    # Ham boyutlandırma
    rm = RiskManager()
    sizing = rm.dynamic_position_size(
        equity=equity,
        entry_price=entry,
        stop_loss_price=sl,
        confidence=confidence,
        regime=regime,
    )
    qty = int(sizing.get("qty", 0))
    risk_amount = _to_float(sizing.get("risk_amount", 0))

    # Pozisyon büyüklüğü TRY
    size_try = qty * entry
    position_after_pct = (size_try / equity * 100) if equity else 0

    # Mesafeler
    stop_dist_pct = abs(entry - sl) / entry * 100 if entry else 0
    reward_dist_pct = (tp - entry) / entry * 100 if entry and tp else 0
    rr_ratio = (reward_dist_pct / stop_dist_pct) if stop_dist_pct else 0

    # Sektör yoğunluğu (bu sinyal eklenirse)
    sector = _ticker_sector(ticker)
    sector_value = 0.0
    for p in positions:
        ps = _ticker_sector(p.get("ticker", ""))
        if ps == sector:
            qty_p = _to_float(p.get("qty"))
            price_p = _to_float(p.get("current_price") or p.get("avg_entry") or p.get("avg_entry_price"))
            sector_value += qty_p * price_p
    sector_value_after = sector_value + size_try
    sector_after_pct = (sector_value_after / equity * 100) if equity else 0

    # Yatırım yoğunluğu (regime tarafından sınırlı)
    invested = sum(
        _to_float(p.get("qty")) * _to_float(p.get("current_price") or p.get("avg_entry") or p.get("avg_entry_price"))
        for p in positions
    )
    invested_after = invested + size_try
    invested_after_pct = (invested_after / equity * 100) if equity else 0
    regime_max = REGIME_MAX_INVESTED.get(regime, 70) if isinstance(REGIME_MAX_INVESTED, dict) else 70

    # ─── Risk Skoru hesabı ─────────────────────────────────────
    score = 0  # 0=güvenli, 100=tehlikeli

    # Confidence düşükse skor artar
    if confidence < 6:
        score += 25
        warnings.append(f"Düşük güven ({confidence}/10) — risk artar")
    elif confidence < 8:
        score += 10

    # R/R oranı zayıfsa
    if rr_ratio < 1.5 and rr_ratio > 0:
        score += 20
        warnings.append(f"R/R 1:{rr_ratio:.2f} — minimum 1:2 hedeflenmeli")
    elif rr_ratio < 2:
        score += 8

    # Stop çok uzaksa (genelde yanlış SL)
    if stop_dist_pct > 4:
        score += 15
        warnings.append(f"Stop mesafesi %{stop_dist_pct:.1f} — fazla uzak (max %3 önerilir)")

    # Cash yetersizse
    if size_try > cash:
        score += 30
        warnings.append(f"Nakit yetersiz — gereken ₺{size_try:,.0f}, mevcut ₺{cash:,.0f}")

    # Pozisyon limiti
    if position_after_pct > MAX_POSITION_PCT * 100:
        score += 25
        warnings.append(f"Pozisyon portföyün %{position_after_pct:.1f}'i — max %{MAX_POSITION_PCT*100:.0f}")

    # Sektör limiti
    if sector_after_pct > MAX_SECTOR_PCT * 100:
        score += 20
        warnings.append(f"{sector} sektörü %{sector_after_pct:.1f} — max %{MAX_SECTOR_PCT*100:.0f}")

    # Rejim max yatırım
    if invested_after_pct > regime_max:
        score += 15
        warnings.append(f"Rejim '{regime}' için max %{regime_max:.0f} yatırım — sonra %{invested_after_pct:.1f} olur")

    # Risk amount equity'ye oranı
    risk_pct_of_equity = (risk_amount / equity * 100) if equity else 0
    if risk_pct_of_equity > MAX_RISK_PCT * 100 + 0.1:
        score += 25
        warnings.append(f"Risk %{risk_pct_of_equity:.2f} max %{MAX_RISK_PCT*100:.1f}'i geçiyor")

    score = min(score, 100)

    if score >= 60:
        verdict = "RED"
    elif score >= 30:
        verdict = "YELLOW"
    else:
        verdict = "GREEN"

    return {
        "verdict": verdict,
        "risk_score": score,
        "warnings": warnings,
        "recommended_size_try": round(size_try, 2),
        "recommended_qty": qty,
        "stop_distance_pct": round(stop_dist_pct, 2),
        "reward_distance_pct": round(reward_dist_pct, 2),
        "rr_ratio": round(rr_ratio, 2),
        "sector": sector,
        "sector_after_pct": round(sector_after_pct, 2),
        "position_after_pct": round(position_after_pct, 2),
        "risk_amount_try": round(risk_amount, 2),
        "risk_pct_of_equity": round(risk_pct_of_equity, 3),
        "regime_max_invested_pct": regime_max,
        "invested_after_pct": round(invested_after_pct, 2),
    }


# ──────────────────────────────────────────────────────────────────
# Portföy seviyesi risk durumu
# ──────────────────────────────────────────────────────────────────

def assess_portfolio(
    positions: list,
    equity: float,
    cash: float = 0,
    regime: str = "neutral",
) -> dict:
    """Mevcut portföyün risk fotoğrafı."""
    equity = _to_float(equity)
    cash = _to_float(cash)
    invested_value = 0.0
    sector_breakdown: dict[str, float] = {}
    pos_count = 0

    for p in positions or []:
        qty = _to_float(p.get("qty"))
        price = _to_float(p.get("current_price") or p.get("avg_entry") or p.get("avg_entry_price"))
        val = qty * price
        invested_value += val
        sector = _ticker_sector(p.get("ticker", ""))
        sector_breakdown[sector] = sector_breakdown.get(sector, 0) + val
        if qty > 0:
            pos_count += 1

    invested_pct = (invested_value / equity * 100) if equity else 0
    cash_pct = (cash / equity * 100) if equity else 0
    sector_pcts = {s: round(v / equity * 100, 2) for s, v in sector_breakdown.items()} if equity else {}

    regime_max = REGIME_MAX_INVESTED.get(regime, 70) if isinstance(REGIME_MAX_INVESTED, dict) else 70

    warnings: list[str] = []
    score = 0

    if invested_pct > regime_max:
        score += 25
        warnings.append(f"Yatırım oranı %{invested_pct:.1f} > rejim max %{regime_max:.0f}")

    biggest_sector = max(sector_pcts.items(), key=lambda x: x[1]) if sector_pcts else (None, 0)
    if biggest_sector[1] > MAX_SECTOR_PCT * 100:
        score += 20
        warnings.append(f"{biggest_sector[0]} %{biggest_sector[1]:.1f} — max %{MAX_SECTOR_PCT*100:.0f}")

    if pos_count > 8:
        score += 10
        warnings.append(f"{pos_count} açık pozisyon — fazla parçalanmış olabilir")
    elif pos_count == 0:
        # Hiç pozisyon yok — risk düşük ama "fırsat kaçırma" notu
        pass

    if equity <= 0:
        score = 100
        warnings.append("Equity sıfır")

    score = min(score, 100)
    if score >= 60:
        verdict = "RED"
    elif score >= 30:
        verdict = "YELLOW"
    else:
        verdict = "GREEN"

    return {
        "verdict": verdict,
        "risk_score": score,
        "warnings": warnings,
        "equity": equity,
        "cash": cash,
        "invested_value_try": round(invested_value, 2),
        "invested_pct": round(invested_pct, 2),
        "cash_pct": round(cash_pct, 2),
        "position_count": pos_count,
        "sector_breakdown_pct": sector_pcts,
        "biggest_sector": {"name": biggest_sector[0], "pct": biggest_sector[1]},
        "regime_max_invested_pct": regime_max,
    }


# ──────────────────────────────────────────────────────────────────
# Simülasyon: sinyal eklenirse ne olur?
# ──────────────────────────────────────────────────────────────────

def simulate_signal_addition(
    signal: dict,
    portfolio: dict,
    regime: str = "neutral",
) -> dict:
    """Sinyali ekleyip kaldırarak before/after delta üretir."""
    before = assess_portfolio(
        positions=portfolio.get("positions") or [],
        equity=portfolio.get("equity") or 0,
        cash=portfolio.get("cash") or 0,
        regime=regime,
    )
    sig_assess = assess_signal(signal, portfolio, regime=regime)

    qty = sig_assess.get("recommended_qty", 0)
    entry = _entry_midpoint(signal)
    if qty > 0 and entry > 0:
        synthetic_position = {
            "ticker": signal.get("ticker") or signal.get("symbol", ""),
            "qty": qty,
            "current_price": entry,
            "avg_entry": entry,
        }
        new_positions = (portfolio.get("positions") or []) + [synthetic_position]
        new_cash = max(0, _to_float(portfolio.get("cash")) - qty * entry)
        after = assess_portfolio(
            positions=new_positions,
            equity=portfolio.get("equity") or 0,
            cash=new_cash,
            regime=regime,
        )
    else:
        after = before

    return {
        "signal_assessment": sig_assess,
        "portfolio_before": before,
        "portfolio_after": after,
        "delta": {
            "invested_pct_change": round(after["invested_pct"] - before["invested_pct"], 2),
            "cash_pct_change": round(after["cash_pct"] - before["cash_pct"], 2),
            "risk_score_change": after["risk_score"] - before["risk_score"],
        },
    }


# ──────────────────────────────────────────────────────────────────
# Council toplam görüş
# ──────────────────────────────────────────────────────────────────

def council_view(
    signals: list,
    portfolio: dict,
    regime: str = "neutral",
    audit_results: list | None = None,
) -> dict:
    """
    Tüm aday sinyallerin Risk Agent'tan geçmiş halini ve Gemini Council
    sonucunu birleştirir. AI Council dashboard view'i bu çıktıyı kullanır.

    Returns:
        {
          "summary": {greens, yellows, reds},
          "items": [{
              ticker, claude_action, claude_confidence,
              risk: {verdict, score, warnings, recommended_qty, ...},
              gemini: {verdict, reasoning} | None,
              final_verdict: GREEN | YELLOW | RED,
              final_reasoning: str
          }, ...]
        }
    """
    audit_by_ticker = {}
    for r in (audit_results or []):
        t = r.get("ticker")
        if t:
            audit_by_ticker[t] = r

    items = []
    greens = yellows = reds = 0

    for sig in signals or []:
        ticker = sig.get("ticker") or sig.get("symbol") or ""
        risk = assess_signal(sig, portfolio, regime=regime)
        gemini = audit_by_ticker.get(ticker)

        # Final verdict: en kötü sonuç kazanır
        priorities = {"RED": 3, "YELLOW": 2, "GREEN": 1, "APPROVE": 1, "REJECT": 3, "MODIFY": 2}
        risk_p = priorities.get(risk["verdict"], 2)
        gem_p = priorities.get((gemini or {}).get("audit_verdict", "APPROVE"), 1)
        worst = max(risk_p, gem_p)
        final = "RED" if worst == 3 else "YELLOW" if worst == 2 else "GREEN"

        # Gerekçe
        reasons = []
        if risk["warnings"]:
            reasons.extend(risk["warnings"][:2])
        if gemini and gemini.get("audit_verdict") in ("REJECT", "MODIFY"):
            gv = gemini.get("audit_verdict")
            gr = (gemini.get("reasoning") or "")[:120]
            reasons.append(f"Gemini {gv}: {gr}")
        if not reasons and final == "GREEN":
            reasons.append(f"Tüm filtreler geçildi (güven {sig.get('confidence')}/10, R/R 1:{risk['rr_ratio']})")

        item = {
            "ticker": ticker,
            "claude_action": sig.get("action"),
            "claude_confidence": sig.get("confidence"),
            "claude_strategy": sig.get("strategy"),
            "claude_reasoning": (sig.get("reasoning") or "")[:300],
            "risk": risk,
            "gemini": (
                {
                    "verdict": gemini.get("audit_verdict"),
                    "reasoning": (gemini.get("reasoning") or "")[:300],
                    "risk_flags": gemini.get("risk_flags", []),
                }
                if gemini else None
            ),
            "final_verdict": final,
            "final_reasoning": " | ".join(reasons) if reasons else "—",
            "expected_exit": sig.get("expected_exit"),
            "max_hold_minutes": sig.get("max_hold_minutes"),
        }
        items.append(item)

        if final == "GREEN":
            greens += 1
        elif final == "YELLOW":
            yellows += 1
        else:
            reds += 1

    return {
        "summary": {"green": greens, "yellow": yellows, "red": reds, "total": len(items)},
        "items": items,
    }
