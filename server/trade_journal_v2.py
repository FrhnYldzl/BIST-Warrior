"""
trade_journal_v2.py — Advanced Trade Journal (V5 Faz 5)

Mevcut trade_journal.py uzerine gelismis ozellikler ekler:
  - Islem etiketleri (tags) ve notlar
  - Islem gruplama (setup turleri)
  - Detayli performans analizi (gun/saat bazli, setup bazli)
  - Kazanan/kaybeden pattern analizi
  - Streak takibi (art arda kazanc/kayip)
  - Emotional / psikolojik istatistikler
  - CSV export

Mevcut journal tablosunu genisletir, geriye donuk uyumlu.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "trades.db"


def init_journal_v2():
    """V2 journal tablolarini olustur."""
    with sqlite3.connect(DB_PATH) as conn:
        # Journal_v2 tablosu — daha detayli
        conn.execute("""
            CREATE TABLE IF NOT EXISTS journal_v2 (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,
                side TEXT DEFAULT 'long',
                entry_price REAL,
                exit_price REAL,
                qty REAL DEFAULT 0,
                pnl REAL DEFAULT 0,
                pnl_pct REAL DEFAULT 0,
                holding_minutes INTEGER DEFAULT 0,
                setup_type TEXT DEFAULT '',
                tags TEXT DEFAULT '[]',
                notes TEXT DEFAULT '',
                ai_confidence INTEGER DEFAULT 0,
                ai_prediction TEXT DEFAULT '',
                regime TEXT DEFAULT '',
                strategy TEXT DEFAULT '',
                entry_reason TEXT DEFAULT '',
                exit_reason TEXT DEFAULT '',
                stop_loss REAL,
                take_profit REAL,
                risk_reward REAL,
                slippage_pct REAL DEFAULT 0,
                market_session TEXT DEFAULT '',
                pre_trade_emotion TEXT DEFAULT '',
                post_trade_emotion TEXT DEFAULT '',
                lesson TEXT DEFAULT '',
                screenshot_url TEXT DEFAULT '',
                council_verdict TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)

        # Tags tablosu
        conn.execute("""
            CREATE TABLE IF NOT EXISTS journal_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag TEXT UNIQUE NOT NULL,
                color TEXT DEFAULT '#3b82f6',
                trade_count INTEGER DEFAULT 0,
                win_count INTEGER DEFAULT 0,
                total_pnl REAL DEFAULT 0
            )
        """)

        conn.commit()


def log_trade_v2(
    ticker: str,
    action: str,
    side: str = "long",
    entry_price: float = 0,
    exit_price: float = 0,
    qty: float = 0,
    setup_type: str = "",
    tags: list[str] = None,
    notes: str = "",
    ai_confidence: int = 0,
    ai_prediction: str = "",
    regime: str = "",
    strategy: str = "",
    entry_reason: str = "",
    exit_reason: str = "",
    stop_loss: float = None,
    take_profit: float = None,
    slippage_pct: float = 0,
    market_session: str = "",
    council_verdict: str = "",
) -> dict:
    """Gelismis islem kaydı yarat."""

    # P&L hesapla
    pnl = 0
    pnl_pct = 0
    risk_reward = None

    if exit_price and entry_price > 0:
        if side == "long":
            pnl = (exit_price - entry_price) * qty
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl = (entry_price - exit_price) * qty
            pnl_pct = (entry_price - exit_price) / entry_price * 100

    if stop_loss and take_profit and entry_price > 0:
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        risk_reward = round(reward / risk, 2) if risk > 0 else None

    tags_json = json.dumps(tags or [])
    now = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT INTO journal_v2 (
                timestamp, ticker, action, side, entry_price, exit_price, qty,
                pnl, pnl_pct, setup_type, tags, notes, ai_confidence,
                ai_prediction, regime, strategy, entry_reason, exit_reason,
                stop_loss, take_profit, risk_reward, slippage_pct,
                market_session, council_verdict
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            now, ticker, action, side, entry_price, exit_price, qty,
            round(pnl, 2), round(pnl_pct, 2), setup_type, tags_json, notes,
            ai_confidence, ai_prediction, regime, strategy, entry_reason,
            exit_reason, stop_loss, take_profit, risk_reward, slippage_pct,
            market_session, council_verdict,
        ))

        # Tag istatistiklerini guncelle
        for tag in (tags or []):
            conn.execute("""
                INSERT INTO journal_tags (tag, trade_count, win_count, total_pnl)
                VALUES (?, 1, ?, ?)
                ON CONFLICT(tag) DO UPDATE SET
                    trade_count = trade_count + 1,
                    win_count = win_count + ?,
                    total_pnl = total_pnl + ?
            """, (tag, 1 if pnl > 0 else 0, round(pnl, 2),
                  1 if pnl > 0 else 0, round(pnl, 2)))

        conn.commit()

    return {
        "status": "ok",
        "ticker": ticker,
        "action": action,
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
    }


def get_journal_v2(
    limit: int = 50,
    ticker: str = None,
    tag: str = None,
    setup_type: str = None,
    side: str = None,
    winners_only: bool = False,
    losers_only: bool = False,
) -> list[dict]:
    """Filtrelenebilir journal kayitlari getir."""
    query = "SELECT * FROM journal_v2 WHERE 1=1"
    params = []

    if ticker:
        query += " AND ticker = ?"
        params.append(ticker.upper())
    if side:
        query += " AND side = ?"
        params.append(side)
    if setup_type:
        query += " AND setup_type = ?"
        params.append(setup_type)
    if tag:
        query += " AND tags LIKE ?"
        params.append(f'%"{tag}"%')
    if winners_only:
        query += " AND pnl > 0"
    if losers_only:
        query += " AND pnl < 0"

    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]


def get_journal_analytics() -> dict:
    """Kapsamli journal analitikleri."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row

        # Temel metrikler
        rows = conn.execute("""
            SELECT * FROM journal_v2
            WHERE exit_price > 0 AND pnl IS NOT NULL
            ORDER BY timestamp
        """).fetchall()

        if not rows:
            return {"error": "No completed trades in journal", "total_trades": 0}

        trades = [dict(r) for r in rows]
        total = len(trades)
        winners = [t for t in trades if t["pnl"] > 0]
        losers = [t for t in trades if t["pnl"] <= 0]

        win_count = len(winners)
        loss_count = len(losers)
        win_rate = round(win_count / total * 100, 1) if total > 0 else 0

        total_pnl = sum(t["pnl"] for t in trades)
        avg_win = sum(t["pnl"] for t in winners) / win_count if win_count > 0 else 0
        avg_loss = sum(t["pnl"] for t in losers) / loss_count if loss_count > 0 else 0
        profit_factor = round(abs(sum(t["pnl"] for t in winners)) / abs(sum(t["pnl"] for t in losers)), 2) if losers and sum(t["pnl"] for t in losers) != 0 else 0
        expectancy = round(total_pnl / total, 2) if total > 0 else 0

        # Streak analizi
        streaks = _calc_streaks(trades)

        # Setup bazli performans
        setup_stats = _setup_performance(trades)

        # Tag bazli performans
        tag_rows = conn.execute("SELECT * FROM journal_tags ORDER BY trade_count DESC").fetchall()
        tag_stats = [dict(r) for r in tag_rows]
        for ts in tag_stats:
            ts["win_rate"] = round(ts["win_count"] / ts["trade_count"] * 100, 1) if ts["trade_count"] > 0 else 0
            ts["avg_pnl"] = round(ts["total_pnl"] / ts["trade_count"], 2) if ts["trade_count"] > 0 else 0

        # Gun bazli performans
        day_stats = _day_performance(trades)

        # Rejim bazli performans
        regime_stats = _group_performance(trades, "regime")

        # Side performansi (long vs short)
        side_stats = _group_performance(trades, "side")

        # Best/Worst trades
        sorted_by_pnl = sorted(trades, key=lambda t: t["pnl"])
        worst_3 = sorted_by_pnl[:3]
        best_3 = sorted_by_pnl[-3:]

        return {
            "status": "ok",
            "total_trades": total,
            "win_count": win_count,
            "loss_count": loss_count,
            "win_rate": win_rate,
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(avg_win, 2),
            "avg_loss": round(avg_loss, 2),
            "profit_factor": profit_factor,
            "expectancy": expectancy,
            "streaks": streaks,
            "setup_stats": setup_stats,
            "tag_stats": tag_stats[:10],
            "day_stats": day_stats,
            "regime_stats": regime_stats,
            "side_stats": side_stats,
            "best_trades": [_trade_summary(t) for t in best_3],
            "worst_trades": [_trade_summary(t) for t in worst_3],
        }


def _trade_summary(t: dict) -> dict:
    return {
        "ticker": t["ticker"],
        "side": t.get("side", ""),
        "pnl": t["pnl"],
        "pnl_pct": t.get("pnl_pct", 0),
        "setup_type": t.get("setup_type", ""),
        "timestamp": t["timestamp"],
    }


def _calc_streaks(trades: list[dict]) -> dict:
    """Art arda kazanc/kayip serilerini hesapla."""
    if not trades:
        return {}

    current_streak = 0
    max_win_streak = 0
    max_loss_streak = 0
    current_type = None

    for t in trades:
        is_win = t["pnl"] > 0
        if current_type is None:
            current_type = is_win
            current_streak = 1
        elif is_win == current_type:
            current_streak += 1
        else:
            current_type = is_win
            current_streak = 1

        if is_win and current_streak > max_win_streak:
            max_win_streak = current_streak
        elif not is_win and current_streak > max_loss_streak:
            max_loss_streak = current_streak

    return {
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "current_streak": current_streak,
        "current_type": "win" if current_type else "loss",
    }


def _setup_performance(trades: list[dict]) -> list[dict]:
    """Setup turleri bazinda performans."""
    setups = {}
    for t in trades:
        s = t.get("setup_type", "") or "unknown"
        if s not in setups:
            setups[s] = {"trades": 0, "wins": 0, "total_pnl": 0}
        setups[s]["trades"] += 1
        if t["pnl"] > 0:
            setups[s]["wins"] += 1
        setups[s]["total_pnl"] += t["pnl"]

    result = []
    for name, data in setups.items():
        result.append({
            "setup": name,
            "trades": data["trades"],
            "win_rate": round(data["wins"] / data["trades"] * 100, 1) if data["trades"] > 0 else 0,
            "total_pnl": round(data["total_pnl"], 2),
            "avg_pnl": round(data["total_pnl"] / data["trades"], 2) if data["trades"] > 0 else 0,
        })

    return sorted(result, key=lambda x: x["total_pnl"], reverse=True)


def _day_performance(trades: list[dict]) -> dict:
    """Haftanin gunlerine gore performans."""
    days = {}
    for t in trades:
        try:
            dt = datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00"))
            day_name = dt.strftime("%A")
        except Exception:
            day_name = "Unknown"

        if day_name not in days:
            days[day_name] = {"trades": 0, "wins": 0, "total_pnl": 0}
        days[day_name]["trades"] += 1
        if t["pnl"] > 0:
            days[day_name]["wins"] += 1
        days[day_name]["total_pnl"] += t["pnl"]

    result = {}
    for day, data in days.items():
        result[day] = {
            "trades": data["trades"],
            "win_rate": round(data["wins"] / data["trades"] * 100, 1) if data["trades"] > 0 else 0,
            "total_pnl": round(data["total_pnl"], 2),
        }
    return result


def _group_performance(trades: list[dict], key: str) -> dict:
    """Herhangi bir alana gore gruplu performans."""
    groups = {}
    for t in trades:
        g = t.get(key, "") or "unknown"
        if g not in groups:
            groups[g] = {"trades": 0, "wins": 0, "total_pnl": 0}
        groups[g]["trades"] += 1
        if t["pnl"] > 0:
            groups[g]["wins"] += 1
        groups[g]["total_pnl"] += t["pnl"]

    result = {}
    for name, data in groups.items():
        result[name] = {
            "trades": data["trades"],
            "win_rate": round(data["wins"] / data["trades"] * 100, 1) if data["trades"] > 0 else 0,
            "total_pnl": round(data["total_pnl"], 2),
        }
    return result


def export_journal_csv() -> str:
    """Journal'i CSV string olarak export et."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM journal_v2 ORDER BY timestamp DESC").fetchall()

    if not rows:
        return "No data"

    headers = list(dict(rows[0]).keys())
    lines = [",".join(headers)]
    for row in rows:
        d = dict(row)
        vals = []
        for h in headers:
            v = str(d.get(h, ""))
            if "," in v or '"' in v:
                v = '"' + v.replace('"', '""') + '"'
            vals.append(v)
        lines.append(",".join(vals))

    return "\n".join(lines)
