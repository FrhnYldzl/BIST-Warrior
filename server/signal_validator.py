"""
signal_validator.py — Claude Sinyal Kalite Doğrulayıcısı (Sprint 3)

Claude prompt'u sıkı kurallar koyuyor ama LLM bazen bu kuralları yanlış
uygular: entry_zone tepeden çıkar, TP yüzeyel olur, SL çok uzakta kalır,
fiyat değişikliği nedeniyle sinyal stale olur.

Validator her decision için piyasa verisiyle KIYASLAR ve `quality_flags`
listesi koyar. Risk Agent bu flag'leri verdict'e yansıtır.

Quality flags:
  • stale_entry         → entry_high > current × 1.001 (Claude tepeden girmiş)
  • deep_entry          → entry_low < current × 0.97 (gerçekçi olmayan derin)
  • shallow_alpha       → TP - current < %1.5 (gerçek alpha çok az)
  • tp_already_hit      → current ≥ TP (TP zaten geçilmiş)
  • sl_too_far          → SL mesafesi > %3 (çok uzak)
  • sl_too_close        → SL mesafesi < ATR_pct × 0.5 (gürültüde tetiklenir)
  • sl_invalid          → SL ≥ entry (long için anlamsız)
  • rr_too_low          → R/R < 1.5
  • price_drift         → Claude'un "current_price_at_signal"ı gerçek
                          piyasadan %0.5+ saparsa (eski veri kullanmış)
  • passed              → tüm gate'ler temiz
"""

from __future__ import annotations

from typing import Optional


def _to_float(v, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        if isinstance(v, str):
            v = v.replace(",", ".").replace("₺", "").strip()
        return float(v)
    except Exception:
        return default


def _entry_bounds(zone) -> tuple[Optional[float], Optional[float]]:
    """'42.50-43.00' → (42.50, 43.00). Negatif sayı yok."""
    if zone is None:
        return (None, None)
    if isinstance(zone, (int, float)):
        v = float(zone)
        return (v, v)
    import re
    s = str(zone).replace(",", ".").replace("₺", "").strip()
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        return (None, None)
    if len(nums) == 1:
        v = float(nums[0])
        return (v, v)
    a, b = float(nums[0]), float(nums[1])
    return (min(a, b), max(a, b))


def validate_signal(decision: dict, market_data: dict | None = None) -> dict:
    """
    Tek decision için kalite kontrolü.

    Args:
        decision: Claude'dan gelen sinyal {ticker, action, entry_zone, stop_loss, take_profit, ...}
        market_data: get_market_data() çıktısı — {ticker: {price, atr_pct, ...}}

    Returns:
        {
          "passed": bool,
          "score": 0-100,             # 100 = mükemmel, 0 = kullanılamaz
          "flags": ["stale_entry", ...],
          "explanations": ["entry_high X > current Y × 1.001", ...],
          "metrics": {alpha_to_tp_pct, sl_distance_pct, rr_ratio, ...}
        }
    """
    if (decision or {}).get("action") not in ("long", "close_long", "short"):
        # watch / hold kararları validator'dan muaf
        return {"passed": True, "score": 100, "flags": ["non_actionable"], "explanations": [], "metrics": {}}

    ticker = decision.get("ticker") or decision.get("symbol", "")
    entry_low, entry_high = _entry_bounds(decision.get("entry_zone") or decision.get("entry"))
    sl = _to_float(decision.get("stop_loss"))
    tp = _to_float(decision.get("take_profit"))
    claude_current = _to_float(decision.get("current_price_at_signal"))

    md = (market_data or {}).get(ticker, {}) if market_data else {}
    real_current = _to_float(md.get("price"))
    atr_pct = _to_float(md.get("atr_pct")) or 1.5  # default %1.5

    # Reference fiyat: gerçek market_data tercih, yoksa Claude'unki
    ref = real_current or claude_current
    if ref <= 0:
        return {
            "passed": False, "score": 30,
            "flags": ["no_reference_price"],
            "explanations": ["Reference fiyat bulunamadı — validator çalıştırılamadı"],
            "metrics": {},
        }

    flags: list[str] = []
    explanations: list[str] = []

    # ─── 1. Stale entry — Claude tepeden girmiş ────────────────
    if entry_high and entry_high > ref * 1.001:
        flags.append("stale_entry")
        delta = (entry_high / ref - 1) * 100
        explanations.append(f"entry_high ₺{entry_high:.2f} > current ₺{ref:.2f} (%{delta:+.2f}) — sinyal tepeden")

    # ─── 2. Deep entry — gerçekçi olmayan derin ───────────────
    if entry_low and entry_low < ref * 0.97:
        flags.append("deep_entry")
        delta = (entry_low / ref - 1) * 100
        explanations.append(f"entry_low ₺{entry_low:.2f} current'tan %{delta:.2f} aşağıda — fırsat kaçar")

    # ─── 3. Alpha kontrolü — TP - current ─────────────────────
    if tp > 0:
        alpha_pct = (tp - ref) / ref * 100
        if alpha_pct < 0:
            flags.append("tp_already_hit")
            explanations.append(f"TP ₺{tp:.2f} ≤ current ₺{ref:.2f} — hedef zaten geçildi")
        elif alpha_pct < 1.5:
            flags.append("shallow_alpha")
            explanations.append(f"TP'ye %{alpha_pct:.2f} kaldı — minimum %1.5 alpha gerekli")

    # ─── 4. SL mesafesi ────────────────────────────────────────
    if sl > 0:
        # Long mantığı
        if entry_low and sl >= entry_low:
            flags.append("sl_invalid")
            explanations.append(f"SL ₺{sl:.2f} ≥ entry_low ₺{entry_low:.2f} — long için anlamsız")
        else:
            entry_mid = (entry_low + entry_high) / 2 if (entry_low and entry_high) else ref
            sl_dist_pct = (entry_mid - sl) / entry_mid * 100 if entry_mid else 0
            if sl_dist_pct > 3.0:
                flags.append("sl_too_far")
                explanations.append(f"SL mesafesi %{sl_dist_pct:.2f} — fazla uzak (max %3 önerilir)")
            elif sl_dist_pct < atr_pct * 0.5 and sl_dist_pct > 0:
                flags.append("sl_too_close")
                explanations.append(f"SL mesafesi %{sl_dist_pct:.2f} < ATR×0.5 (%{atr_pct*0.5:.2f}) — gürültüde tetiklenir")

    # ─── 5. R/R hesabı ─────────────────────────────────────────
    rr_ratio = None
    if entry_high and tp and sl > 0:
        entry_mid = (entry_low + entry_high) / 2 if (entry_low and entry_high) else entry_high
        reward = tp - entry_mid
        risk = entry_mid - sl
        if risk > 0:
            rr_ratio = reward / risk
            if rr_ratio < 1.5:
                flags.append("rr_too_low")
                explanations.append(f"R/R 1:{rr_ratio:.2f} < 1:1.5 minimum")

    # ─── 6. Price drift — Claude'un baktığı fiyat ile şu an ────
    if claude_current and real_current and claude_current > 0 and real_current > 0:
        drift_pct = abs(real_current / claude_current - 1) * 100
        if drift_pct > 0.5:
            flags.append("price_drift")
            explanations.append(
                f"Claude {claude_current:.2f}'da analiz etti, şu an {real_current:.2f} (%{drift_pct:.2f} sapma) — sinyal güncelliği şüpheli"
            )

    # ─── Skorlama ─────────────────────────────────────────────
    weight_map = {
        "tp_already_hit":   -45,
        "stale_entry":      -25,
        "shallow_alpha":    -25,
        "sl_invalid":       -40,
        "sl_too_far":       -15,
        "sl_too_close":     -10,
        "rr_too_low":       -20,
        "price_drift":      -15,
        "deep_entry":       -10,
        "no_reference_price": -30,
    }
    score = 100 + sum(weight_map.get(f, 0) for f in flags)
    score = max(0, min(100, score))

    if not flags:
        flags.append("passed")

    passed = ("tp_already_hit" not in flags
              and "sl_invalid" not in flags
              and "no_reference_price" not in flags
              and score >= 50)

    metrics = {}
    if tp and ref:
        metrics["alpha_to_tp_pct"] = round((tp - ref) / ref * 100, 2)
    if sl and entry_high:
        entry_mid = (entry_low + entry_high) / 2 if (entry_low and entry_high) else entry_high
        metrics["sl_distance_pct"] = round((entry_mid - sl) / entry_mid * 100, 2) if entry_mid else 0
    if rr_ratio is not None:
        metrics["rr_ratio"] = round(rr_ratio, 2)
    if claude_current and real_current:
        metrics["price_drift_pct"] = round((real_current / claude_current - 1) * 100, 2)
    metrics["reference_price"] = round(ref, 2)
    metrics["atr_pct"] = round(atr_pct, 2)

    return {
        "passed": passed,
        "score": score,
        "flags": flags,
        "explanations": explanations,
        "metrics": metrics,
    }


def annotate_decisions(decisions: list, market_data: dict | None = None) -> list[dict]:
    """Her decision'a quality field'ı ekleyerek liste döndürür (in-place değil)."""
    out = []
    for d in decisions or []:
        copy = dict(d)
        copy["quality"] = validate_signal(d, market_data=market_data)
        out.append(copy)
    return out


# Council'da kısa flag etiketleri
FLAG_LABELS = {
    "passed":             ("✓", "Geçti", "var(--success)"),
    "stale_entry":        ("⚠", "Tepeden giriş", "var(--warning)"),
    "deep_entry":         ("ℹ", "Çok derin", "var(--muted)"),
    "shallow_alpha":      ("⚠", "Düşük alpha", "var(--warning)"),
    "tp_already_hit":     ("✗", "TP geçildi", "var(--danger)"),
    "sl_too_far":         ("⚠", "SL uzak", "var(--warning)"),
    "sl_too_close":       ("⚠", "SL çok yakın", "var(--warning)"),
    "sl_invalid":         ("✗", "SL geçersiz", "var(--danger)"),
    "rr_too_low":         ("⚠", "R/R zayıf", "var(--warning)"),
    "price_drift":        ("⚠", "Fiyat saptı", "var(--warning)"),
    "no_reference_price": ("ℹ", "Veri yok", "var(--muted)"),
    "non_actionable":     ("·", "Watch", "var(--muted)"),
}
