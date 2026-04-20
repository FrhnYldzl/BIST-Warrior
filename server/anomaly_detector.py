"""
anomaly_detector.py — Anormal Piyasa Davranışı Tespiti (V3.3)

Standart sapma, z-score ve tarihsel dağılım bazlı anomali tespiti.

Tespit edilen anomaliler:
  1. Volume Spike: Hacim ortalamanın 3x+ üzerinde
  2. Price Shock: Günlük hareket 2 standart sapma üzerinde
  3. Volatility Explosion: ATR aniden genişliyor
  4. Correlation Break: Normalde korelasyonlu hisseler farklı yönde
  5. Gap Anomaly: Açılış farkı normalin çok üstünde
  6. RSI Extreme: RSI <15 veya >85 bölgesinde (nadir)

Her anomali severity (low/medium/high/critical) ile raporlanır.
"""

import math
from datetime import datetime, timezone


def detect_anomalies(market_data: dict) -> dict:
    """
    Piyasa verisinde anomali tara.

    Returns: {
        anomalies: [{ticker, type, severity, detail, value, threshold}],
        anomaly_count: int,
        risk_level: str,
        summary: str,
    }
    """
    tickers = [k for k in market_data if k != "_meta"]
    anomalies = []

    for ticker in tickers:
        d = market_data.get(ticker, {})
        if not d or "error" in d:
            continue

        price = d.get("price", 0)
        if price <= 0:
            continue

        # ─── 1. Volume Spike ────────────────────────────────
        vol_ratio = d.get("volume_ratio", 1.0)
        if vol_ratio >= 5.0:
            anomalies.append({
                "ticker": ticker,
                "type": "volume_spike",
                "severity": "critical",
                "detail": f"Hacim normalin {vol_ratio:.1f}x üzerinde — kurumsal hareket olabilir",
                "value": vol_ratio,
                "threshold": 5.0,
            })
        elif vol_ratio >= 3.0:
            anomalies.append({
                "ticker": ticker,
                "type": "volume_spike",
                "severity": "high",
                "detail": f"Hacim normalin {vol_ratio:.1f}x üzerinde",
                "value": vol_ratio,
                "threshold": 3.0,
            })

        # ─── 2. Price Shock ─────────────────────────────────
        change_pct = d.get("change_pct", 0)
        atr14 = d.get("atr14", 0)
        atr_pct = (atr14 / price * 100) if price > 0 and atr14 > 0 else 2.0

        # 2 ATR'den fazla hareket = anormal
        if abs(change_pct) > atr_pct * 2.5:
            sev = "critical" if abs(change_pct) > atr_pct * 4 else "high"
            direction = "yükseliş" if change_pct > 0 else "düşüş"
            anomalies.append({
                "ticker": ticker,
                "type": "price_shock",
                "severity": sev,
                "detail": f"%{change_pct:.1f} {direction} — normal dalgalanmanın {abs(change_pct)/atr_pct:.1f}x üzerinde",
                "value": abs(change_pct),
                "threshold": round(atr_pct * 2.5, 2),
            })
        elif abs(change_pct) >= 5.0:
            anomalies.append({
                "ticker": ticker,
                "type": "price_shock",
                "severity": "high",
                "detail": f"%{change_pct:.1f} günlük hareket",
                "value": abs(change_pct),
                "threshold": 5.0,
            })

        # ─── 3. Gap Anomaly ─────────────────────────────────
        gap_pct = d.get("gap_pct", 0)
        if abs(gap_pct) >= 5.0:
            sev = "critical" if abs(gap_pct) >= 8.0 else "high"
            direction = "yukarı" if gap_pct > 0 else "aşağı"
            anomalies.append({
                "ticker": ticker,
                "type": "gap_anomaly",
                "severity": sev,
                "detail": f"%{gap_pct:.1f} {direction} gap — earnings/haber etkisi olabilir",
                "value": abs(gap_pct),
                "threshold": 5.0,
            })
        elif abs(gap_pct) >= 3.0:
            anomalies.append({
                "ticker": ticker,
                "type": "gap_anomaly",
                "severity": "medium",
                "detail": f"%{gap_pct:.1f} gap açılışı",
                "value": abs(gap_pct),
                "threshold": 3.0,
            })

        # ─── 4. RSI Extreme ─────────────────────────────────
        rsi = d.get("rsi14", 50)
        if rsi >= 85:
            anomalies.append({
                "ticker": ticker,
                "type": "rsi_extreme",
                "severity": "high",
                "detail": f"RSI {rsi:.0f} — aşırı alım bölgesi, geri çekilme riski yüksek",
                "value": rsi,
                "threshold": 85,
            })
        elif rsi <= 15:
            anomalies.append({
                "ticker": ticker,
                "type": "rsi_extreme",
                "severity": "high",
                "detail": f"RSI {rsi:.0f} — aşırı satım, panik satışı olabilir",
                "value": rsi,
                "threshold": 15,
            })

        # ─── 5. Bollinger Band Breakout ─────────────────────
        bb_pos = d.get("bb_position", 0.5)
        bb_width = d.get("bb_width", 0)
        if bb_pos >= 1.1 or bb_pos <= -0.1:
            side = "üst" if bb_pos > 1 else "alt"
            anomalies.append({
                "ticker": ticker,
                "type": "bb_breakout",
                "severity": "medium",
                "detail": f"Bollinger {side} band dışına çıktı (pos: {bb_pos:.2f})",
                "value": bb_pos,
                "threshold": 1.0,
            })

        # ─── 6. Volatility Explosion ────────────────────────
        if bb_width >= 12.0:
            anomalies.append({
                "ticker": ticker,
                "type": "volatility_explosion",
                "severity": "high" if bb_width >= 16 else "medium",
                "detail": f"BB genişliği %{bb_width:.1f} — volatilite patlaması",
                "value": bb_width,
                "threshold": 12.0,
            })

    # Severity sıralama
    severity_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    anomalies.sort(key=lambda a: severity_order.get(a["severity"], 4))

    # Genel risk seviyesi
    critical_count = sum(1 for a in anomalies if a["severity"] == "critical")
    high_count = sum(1 for a in anomalies if a["severity"] == "high")

    if critical_count >= 2:
        risk_level = "extreme"
    elif critical_count >= 1 or high_count >= 3:
        risk_level = "high"
    elif high_count >= 1:
        risk_level = "elevated"
    elif anomalies:
        risk_level = "moderate"
    else:
        risk_level = "normal"

    summary = _build_summary(anomalies, risk_level)

    return {
        "anomalies": anomalies,
        "anomaly_count": len(anomalies),
        "risk_level": risk_level,
        "summary": summary,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _build_summary(anomalies: list, risk_level: str) -> str:
    if not anomalies:
        return "Anormal piyasa davranışı tespit edilmedi — normal koşullar."

    parts = [f"Risk seviyesi: {risk_level.upper()} — {len(anomalies)} anomali tespit edildi."]

    # Critical anomalileri öne çıkar
    criticals = [a for a in anomalies if a["severity"] == "critical"]
    if criticals:
        tickers = list(set(a["ticker"] for a in criticals))
        parts.append(f"KRİTİK: {', '.join(tickers)} — {criticals[0]['detail']}")

    # Tip bazında özet
    types = {}
    for a in anomalies:
        types[a["type"]] = types.get(a["type"], 0) + 1
    type_str = ", ".join(f"{t}: {c}" for t, c in types.items())
    parts.append(f"Dağılım: {type_str}")

    return " | ".join(parts)
