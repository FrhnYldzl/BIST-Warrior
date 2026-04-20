"""
trade_journal.py — Islem Gunlugu ve Ogrenme Hafizasi (V2.2)

Her islem sonrasi:
  1. Claude'un ongorusunu vs gercek sonucu karsilastirir
  2. "Ders cikarimi" uretir (Claude AI ile)
  3. Gecmis derslerden oruntu cikarir
  4. Performans metrikleri hesaplar

Bu, Agent'in "deneyimli" olmasini saglayan hafiza katmanidir.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import os
from dotenv import dotenv_values, load_dotenv

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)
_env_vals = dotenv_values(_env_path)
def _get(key): return os.getenv(key) or _env_vals.get(key, "")

DB_PATH = Path(__file__).parent / "trades.db"


def init_journal_db():
    """Journal tablosunu olustur."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                entry_price REAL,
                exit_price REAL,
                qty REAL,
                pnl REAL,
                pnl_pct REAL,
                ai_prediction TEXT,
                ai_confidence INTEGER,
                actual_outcome TEXT,
                lesson TEXT,
                lesson_type TEXT,
                strategy_used TEXT,
                regime_at_trade TEXT,
                holding_period TEXT,
                slippage_pct REAL
            )
        """)
        # Performans ozeti tablosu
        conn.execute("""
            CREATE TABLE IF NOT EXISTS performance (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                period TEXT NOT NULL,
                total_trades INTEGER,
                win_count INTEGER,
                loss_count INTEGER,
                win_rate REAL,
                total_pnl REAL,
                avg_winner REAL,
                avg_loser REAL,
                profit_factor REAL,
                max_drawdown REAL,
                sharpe_estimate REAL,
                lessons_summary TEXT
            )
        """)
        conn.commit()


def log_journal_entry(
    ticker: str,
    action: str,
    entry_price: float,
    exit_price: float = None,
    qty: float = 0,
    ai_prediction: str = "",
    ai_confidence: int = 0,
    strategy_used: str = "",
    regime: str = "",
) -> dict:
    """
    Islem girisini journal'a kaydet.
    Exit price ve PnL sonradan guncellenir.
    """
    pnl = None
    pnl_pct = None
    if exit_price and entry_price > 0:
        if action in ("long",):
            pnl = (exit_price - entry_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        elif action in ("short",):
            pnl = (entry_price - exit_price) * qty
            pnl_pct = (entry_price - exit_price) / entry_price * 100

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                INSERT INTO journal
                    (timestamp, ticker, action, entry_price, exit_price, qty,
                     pnl, pnl_pct, ai_prediction, ai_confidence,
                     strategy_used, regime_at_trade)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                datetime.now(timezone.utc).isoformat(),
                ticker, action, entry_price, exit_price, qty,
                pnl, pnl_pct, ai_prediction, ai_confidence,
                strategy_used, regime,
            ))
            conn.commit()
        return {"status": "ok", "ticker": ticker}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def generate_lesson(trade_data: dict) -> dict:
    """
    Claude ile islem sonrasi ders cikarimi.
    'Dun AVGO icin VWAP pullback bekledim, geldi mi?
     Geldiginde tepkisi ne oldu?'
    """
    api_key = _get("ANTHROPIC_API_KEY")
    if not api_key:
        return {"lesson": "API key yok", "lesson_type": "skip"}

    client = anthropic.Anthropic(api_key=api_key)

    prompt = f"""You are reviewing a single trade as part of a continuous learning process.

TRADE DATA:
- Ticker: {trade_data.get('ticker', '?')}
- Action: {trade_data.get('action', '?')}
- Entry: ${trade_data.get('entry_price', 0):.2f}
- Exit: ${trade_data.get('exit_price', 'still open')}
- PnL: ${trade_data.get('pnl', 'N/A')}
- AI Prediction: {trade_data.get('ai_prediction', 'N/A')}
- AI Confidence: {trade_data.get('ai_confidence', 'N/A')}/10
- Strategy: {trade_data.get('strategy_used', '?')}
- Market Regime: {trade_data.get('regime', '?')}

Provide a brief trade review. Respond ONLY with JSON:
{{
  "lesson": "1-2 sentence key takeaway from this trade",
  "lesson_type": "positive | negative | neutral",
  "prediction_accuracy": "accurate | partially | inaccurate | pending",
  "what_to_repeat": "1 sentence — what worked",
  "what_to_avoid": "1 sentence — what didn't work",
  "pattern_detected": "any recurring pattern noticed"
}}"""

    try:
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = msg.content[0].text.strip()
        # JSON extract
        start = text.find("{")
        end = text.rfind("}") + 1
        if start != -1 and end > start:
            return json.loads(text[start:end])
        return {"lesson": text[:200], "lesson_type": "neutral"}
    except Exception as e:
        return {"lesson": f"Review hatasi: {str(e)[:80]}", "lesson_type": "error"}


def get_journal_entries(limit: int = 20) -> list:
    """Son N journal girisi."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return []


def calculate_performance(period: str = "all") -> dict:
    """
    Performans metrikleri hesapla.
    Win rate, profit factor, avg winner/loser, max drawdown tahmini.
    """
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM journal WHERE pnl IS NOT NULL ORDER BY id"
            ).fetchall()

        if not rows:
            return {
                "total_trades": 0,
                "message": "Henuz tamamlanmis islem yok",
            }

        trades = [dict(r) for r in rows]
        pnls = [t["pnl"] for t in trades if t["pnl"] is not None]

        if not pnls:
            return {"total_trades": len(trades), "message": "PnL verisi yok"}

        winners = [p for p in pnls if p > 0]
        losers = [p for p in pnls if p < 0]

        total = len(pnls)
        win_count = len(winners)
        loss_count = len(losers)
        win_rate = round(win_count / total * 100, 1) if total > 0 else 0

        total_pnl = sum(pnls)
        avg_winner = sum(winners) / len(winners) if winners else 0
        avg_loser = sum(losers) / len(losers) if losers else 0

        # Profit factor: toplam kazanc / toplam kayip
        gross_profit = sum(winners) if winners else 0
        gross_loss = abs(sum(losers)) if losers else 1
        profit_factor = round(gross_profit / gross_loss, 2) if gross_loss > 0 else float('inf')

        # Max drawdown tahmini (basit cumulative)
        cumulative = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        # Lessons summary
        positive_lessons = [t.get("lesson", "") for t in trades if t.get("lesson_type") == "positive"]
        negative_lessons = [t.get("lesson", "") for t in trades if t.get("lesson_type") == "negative"]

        result = {
            "total_trades": total,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "total_pnl": round(total_pnl, 2),
            "avg_winner": round(avg_winner, 2),
            "avg_loser": round(avg_loser, 2),
            "profit_factor": profit_factor,
            "max_drawdown": round(max_dd, 2),
            "best_trade": round(max(pnls), 2) if pnls else 0,
            "worst_trade": round(min(pnls), 2) if pnls else 0,
            "positive_patterns": positive_lessons[-3:],  # Son 3 olumlu ders
            "negative_patterns": negative_lessons[-3:],  # Son 3 olumsuz ders
        }

        # DB'ye kaydet
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute("""
                    INSERT INTO performance
                        (timestamp, period, total_trades, win_count, loss_count,
                         win_rate, total_pnl, avg_winner, avg_loser, profit_factor,
                         max_drawdown, lessons_summary)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    datetime.now(timezone.utc).isoformat(),
                    period, total, win_count, loss_count,
                    win_rate, total_pnl, avg_winner, avg_loser, profit_factor,
                    max_dd, json.dumps(positive_lessons[-5:] + negative_lessons[-5:]),
                ))
                conn.commit()
        except Exception:
            pass

        return result
    except Exception as e:
        return {"error": str(e)}


def get_learning_context(limit: int = 5) -> str:
    """
    Claude brain'e gecmis derslerden ozet saglayan context.
    Brain promptuna eklenerek agent'in deneyimlerinden ogrenmesini saglar.
    """
    entries = get_journal_entries(limit=limit)
    if not entries:
        return ""

    lines = []
    for e in entries:
        if e.get("lesson"):
            outcome = "WIN" if (e.get("pnl") or 0) > 0 else "LOSS" if (e.get("pnl") or 0) < 0 else "OPEN"
            lines.append(
                f"  [{outcome}] {e.get('ticker')} {e.get('action')}: {e.get('lesson')}"
            )

    if not lines:
        return ""

    return "\n## LESSONS FROM PAST TRADES (learn from these)\n" + "\n".join(lines)
