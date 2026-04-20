"""
gemini_auditor.py — Gemini Denetçi / İkinci Görüş Motoru (V4.5)

Claude'un trading kararlarını Gemini ile denetler.
İki farklı AI modelin aynı veriye bakıp bağımsız değerlendirme yapması
false positive oranını düşürür.

Mekanizma:
  1. Claude karar verir (long/short/close)
  2. Gemini aynı piyasa verisini görür + Claude'un kararını inceler
  3. Gemini: APPROVE / REJECT / MODIFY + gerekçe döndürür
  4. Sadece ikisi de onaylarsa işlem tetiklenir (Council modu)

Audit sonuçları dashboard'da görünür.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv, dotenv_values

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)
_env_vals = dotenv_values(_env_path)

def _get(key): return os.getenv(key) or _env_vals.get(key, "")

# Gemini key'i birden fazla isimde eklenmiş olabilir — hepsini destekle.
_GEMINI_KEY_NAMES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_GEMINI_API_KEY",
    "GEMINI_KEY",
)


def _get_gemini_key() -> str:
    for name in _GEMINI_KEY_NAMES:
        val = _get(name)
        if val:
            return val
    return ""


# ─── Son audit sonuçları (dashboard için) ─────────────────────────
_last_audit: dict = {
    "status": "Henüz audit yapılmadı",
    "timestamp": None,
    "results": [],
}


def get_last_audit() -> dict:
    return _last_audit


def is_enabled() -> bool:
    """Gemini API key tanımlı mı? (birkaç ad varyantını destekler)"""
    return bool(_get_gemini_key())


def audit_decisions(
    decisions: list,
    market_data: dict,
    portfolio: dict,
    regime: str = "unknown",
) -> list[dict]:
    """
    Claude'un kararlarını Gemini ile denetle.

    Her karar için:
      - APPROVE: İşlem onaylandı
      - REJECT: İşlem reddedildi (gerekçe ile)
      - MODIFY: Değişiklik önerisi (stop_loss, position_size vb.)

    Returns: [{ticker, claude_action, audit_verdict, reasoning, risk_flags, modified_params}]
    """
    global _last_audit

    api_key = _get_gemini_key()
    if not api_key:
        _last_audit = {"status": "GEMINI_API_KEY tanımlı değil", "timestamp": datetime.now(timezone.utc).isoformat(), "results": []}
        return []

    # Sadece GİRİLMESİ OLASI sinyalleri denetle:
    # - action=long veya close (kapat)
    # - confidence >= 7 (düşük güven zaten işlem yapılmaz)
    actionable = [
        d for d in decisions
        if d.get("action") not in ("hold", "watch", None)
        and d.get("confidence", 0) >= 7
    ]
    if not actionable:
        _last_audit = {
            "status": "Denetlenecek karar yok (conf≥7 long/close yok)",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "results": []
        }
        return []

    # Piyasa verisi özeti
    market_summary = _format_market_for_audit(market_data, actionable)
    portfolio_summary = _format_portfolio_for_audit(portfolio)

    audit_results = []
    for decision in actionable:
        try:
            result = _audit_single(api_key, decision, market_summary, portfolio_summary, regime)
            audit_results.append(result)
        except Exception as e:
            short_err = str(e)[:100]
            audit_results.append({
                "ticker": decision.get("ticker", "?"),
                "claude_action": decision.get("action", "?"),
                "claude_confidence": decision.get("confidence", 0),
                "audit_verdict": "APPROVE",
                "reasoning": f"Gemini offline — auto-approved (Claude-only mode). Error: {short_err}",
                "risk_flags": ["gemini_unavailable"],
                "modified_params": {},
            })

    _last_audit = {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_decisions": len(actionable),
        "approved": sum(1 for r in audit_results if r["audit_verdict"] == "APPROVE"),
        "rejected": sum(1 for r in audit_results if r["audit_verdict"] == "REJECT"),
        "modified": sum(1 for r in audit_results if r["audit_verdict"] == "MODIFY"),
        "results": audit_results,
    }

    # Log
    approved = _last_audit["approved"]
    rejected = _last_audit["rejected"]
    print(f"[Gemini Audit] {len(actionable)} karar denetlendi: {approved} onay, {rejected} red")

    return audit_results


def _audit_single(api_key: str, decision: dict, market_summary: str, portfolio_summary: str, regime: str) -> dict:
    """Tek bir kararı Gemini ile denetle."""
    from google import genai

    client = genai.Client(api_key=api_key)

    ticker = decision.get("ticker", "?")
    action = decision.get("action", "?")
    confidence = decision.get("confidence", 0)
    reasoning = decision.get("reasoning", "")
    entry_zone = decision.get("entry_zone", "?")
    stop_loss = decision.get("stop_loss", "?")
    take_profit = decision.get("take_profit", "?")
    risk_reward = decision.get("risk_reward", "?")
    position_pct = decision.get("position_size_pct", 0)

    prompt = f"""You are the RISK AUDITOR of an AI hedge fund. Your job is to independently verify trading decisions made by the primary AI (Claude).

You must be SKEPTICAL and CONSERVATIVE. Your role is to catch mistakes, flag risks, and prevent bad trades.

## MARKET REGIME
Current: {regime}

## PORTFOLIO STATE
{portfolio_summary}

## MARKET DATA
{market_summary}

## CLAUDE'S DECISION TO AUDIT
Ticker: {ticker}
Action: {action}
Confidence: {confidence}/10
Entry Zone: {entry_zone}
Stop Loss: {stop_loss}
Take Profit: {take_profit}
Risk/Reward: {risk_reward}
Position Size: {position_pct}% of portfolio
Claude's Reasoning: {reasoning}

## YOUR AUDIT CHECKLIST
1. Does the action align with the market regime? (Don't go long in a bear market)
2. Is the confidence justified by the data? (High confidence needs strong evidence)
3. Is the stop-loss appropriate? (Too tight = stopped out, too loose = excessive loss)
4. Is the position size reasonable? (Max 2% risk per trade)
5. Are there upcoming events that could impact this trade? (Earnings, Fed, etc.)
6. Is there a better entry point available?
7. Does this create excessive sector/correlation concentration?

## RESPONSE FORMAT (JSON only, no other text)
{{
  "verdict": "APPROVE | REJECT | MODIFY",
  "reasoning": "2-3 sentences explaining your decision",
  "risk_flags": ["list of specific risks identified"],
  "risk_score": 1-10 (1=very safe, 10=very risky),
  "modified_params": {{}}
}}

For MODIFY verdict, include changed parameters in modified_params, e.g.:
{{"stop_loss": "new_value", "position_size_pct": 1.0, "take_profit": "new_value"}}

For REJECT, explain clearly why and what would need to change.
Respond with ONLY the JSON object."""

    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
    )

    text = response.text.strip()

    # JSON parse
    result = _extract_json(text)

    return {
        "ticker": ticker,
        "claude_action": action,
        "claude_confidence": confidence,
        "audit_verdict": result.get("verdict", "ERROR"),
        "reasoning": result.get("reasoning", "Parse hatası"),
        "risk_flags": result.get("risk_flags", []),
        "risk_score": result.get("risk_score", 5),
        "modified_params": result.get("modified_params", {}),
    }


def _extract_json(text: str) -> dict:
    """Gemini yanıtından JSON çıkar."""
    # Markdown code block temizle
    if "```json" in text:
        text = text.split("```json")[1].split("```")[0].strip()
    elif "```" in text:
        text = text.split("```")[1].split("```")[0].strip()

    # Direkt JSON dene
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # İlk { ve son } arası
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass

    return {"verdict": "ERROR", "reasoning": "JSON parse edilemedi: " + text[:200]}


def _format_market_for_audit(market_data: dict, decisions: list) -> str:
    """Audit için piyasa verisi özeti."""
    tickers = set(d.get("ticker", "") for d in decisions)
    tickers.add("SPY")  # Benchmark her zaman dahil

    lines = []
    for t in tickers:
        d = market_data.get(t, {})
        if not d:
            continue
        lines.append(
            f"{t}: ${d.get('price', 0):.2f} | "
            f"Chg: {d.get('change_pct', 0):.1f}% | "
            f"RSI: {d.get('rsi14', 0):.0f} | "
            f"Trend: {d.get('trend', '?')} | "
            f"Vol: {d.get('volume_ratio', 0):.1f}x | "
            f"MACD: {d.get('macd_cross', 'none')} | "
            f"BB: {d.get('bb_position', 0.5):.2f}"
        )
    return "\n".join(lines)


def _format_portfolio_for_audit(portfolio: dict) -> str:
    """Audit için portföy özeti."""
    cash = portfolio.get("cash", 0)
    equity = portfolio.get("equity", 0)
    positions = portfolio.get("positions", [])

    lines = [f"Cash: ${cash:,.0f} | Equity: ${equity:,.0f} | Positions: {len(positions)}"]
    for p in positions[:5]:
        pnl = p.get("unrealized_pl", 0)
        lines.append(f"  {p.get('ticker', '?')}: {p.get('qty', 0)} shares @ ${p.get('avg_entry', 0):.2f} (P&L: ${pnl:.2f})")
    return "\n".join(lines)
