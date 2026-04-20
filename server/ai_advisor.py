"""
ai_advisor.py — Claude AI Trading Advisor

Her işlemden önce ve sonra Claude'u çağırır:
  1. Trade Analysis  : Sinyali onaylar veya reddeder, gerekçe yazar
  2. Strategy Review : İşlem geçmişini analiz edip Pine Script önerir
  3. Health Check    : Sistem sorunlarını tespit eder
"""

import os
import anthropic
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

# .env dosyası server/ klasörünün bir üstünde
_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)
_env_vals = dotenv_values(_env_path)

def _get_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY") or _env_vals.get("ANTHROPIC_API_KEY", "")

client = anthropic.Anthropic(api_key=_get_key())
AI_APPROVAL_REQUIRED = os.getenv("AI_APPROVAL_REQUIRED", "false").lower() == "true"
MODEL = "claude-sonnet-4-6"


# ──────────────────────────────────────────────────────────────────
# 1. Trade Analizi — her sinyal öncesi çağrılır
# ──────────────────────────────────────────────────────────────────

def analyze_trade(
    ticker: str,
    action: str,
    price: float,
    qty: float,
    balance: float,
    recent_trades: list[dict],
) -> dict:
    """
    Claude'a sinyali değerlendirmesini sor.

    Returns:
        {
          "approved": bool,
          "confidence": "high" | "medium" | "low",
          "reasoning": str,
          "risk_note": str,
          "suggestion": str
        }
    """
    if not is_enabled():
        return _disabled_response()

    # Son 5 işlemi özetle
    trade_summary = "\n".join([
        f"  - {t.get('timestamp','')[:16]} | {t.get('ticker')} | {t.get('action')} | ${t.get('price','?')} | {t.get('status')}"
        for t in recent_trades[:5]
    ]) or "  (henüz işlem yok)"

    trade_value = price * qty
    risk_pct = round((trade_value / balance) * 100, 1) if balance > 0 else 0

    prompt = f"""You are an algorithmic trading analyst. Evaluate this trade signal.

Ticker: {ticker} | Action: {action.upper()} | Price: ${price} | Qty: {qty}
Trade Value: ${trade_value:,.0f} | Portfolio Risk: {risk_pct}% | Balance: ${balance:,.0f}

Recent trades:
{trade_summary}

Rules: max 2% portfolio risk, avoid overexposure, check trend direction.

Respond ONLY with this exact JSON on one line, no other text:
{{"approved":true,"confidence":"high","reasoning":"brief reason in English max 15 words","risk_note":null,"suggestion":null}}"""

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        import json, re
        text = message.content[0].text.strip()
        # Kod bloğu varsa çıkar
        if "```" in text:
            parts = text.split("```")
            for part in parts:
                part = part.strip()
                if part.startswith("json"):
                    part = part[4:].strip()
                if part.startswith("{"):
                    text = part
                    break
        # Direkt JSON bloğunu bul
        match = re.search(r'\{[^{}]*\}', text, re.DOTALL)
        if match:
            text = match.group(0)
        return json.loads(text)
    except Exception as e:
        return {
            "approved": True,
            "confidence": "low",
            "reasoning": f"AI analizi tamamlanamadi: {str(e)[:60]}",
            "risk_note": None,
            "suggestion": None,
        }


# ──────────────────────────────────────────────────────────────────
# 2. Strateji Gözden Geçirme — periyodik çağrılır
# ──────────────────────────────────────────────────────────────────

def review_strategy(trades: list[dict], current_pine: str = "") -> dict:
    """
    İşlem geçmişini analiz edip Pine Script iyileştirme önerileri üretir.

    Returns:
        { "summary": str, "suggestions": list[str], "pine_changes": str | None }
    """
    if not os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_API_KEY") == "BURAYA_ANTHROPIC_API_KEY_YAZ":
        return {"summary": "API anahtarı eksik.", "suggestions": [], "pine_changes": None}

    if not trades:
        return {"summary": "Analiz için yeterli işlem verisi yok.", "suggestions": [], "pine_changes": None}

    wins = sum(1 for t in trades if t.get("status") not in ["error", "cancelled"])
    total = len(trades)
    tickers = list(set(t.get("ticker") for t in trades))

    prompt = f"""Bir EMA Cross algoritmasının işlem geçmişini analiz et.

## İstatistikler
- Toplam işlem: {total}
- Başarılı emir: {wins}
- İşlem yapılan semboller: {', '.join(tickers)}

## Son 10 İşlem
{chr(10).join([f"  {t.get('timestamp','')[:16]} | {t.get('ticker')} | {t.get('action')} | ${t.get('price','?')} | {t.get('status')}" for t in trades[:10]])}

## Mevcut Pine Script Parametreleri
EMA Hızlı: 9, EMA Yavaş: 21, Stop-Loss: %2, Risk/Ödül: 1:2

Analiz yap ve JSON döndür:
{{
  "summary": "genel değerlendirme (2-3 cümle)",
  "suggestions": ["öneri 1", "öneri 2", "öneri 3"],
  "pine_changes": "Pine Script parametresi önerisi veya null"
}}"""

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        import json
        text = message.content[0].text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text)
    except Exception as e:
        return {
            "summary": f"Analiz hatası: {str(e)[:80]}",
            "suggestions": [],
            "pine_changes": None,
        }


# ──────────────────────────────────────────────────────────────────
# Yardımcılar
# ──────────────────────────────────────────────────────────────────

def _disabled_response() -> dict:
    return {
        "approved": True,
        "confidence": "low",
        "reasoning": "AI Advisor devre dışı — ANTHROPIC_API_KEY eksik.",
        "risk_note": None,
        "suggestion": None,
    }


def is_enabled() -> bool:
    key = _get_key()
    return bool(key) and key != "BURAYA_ANTHROPIC_API_KEY_YAZ"
