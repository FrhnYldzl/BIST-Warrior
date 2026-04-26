"""
signal_history.py — Sinyal Tarihçesi ve İsabet Takibi

Her Claude tarama sonucunu kalıcı olarak loglar (ticker, entry_zone, SL, TP,
confidence, strategy, scan_time). Sinyalin yaşam döngüsünü izler:

  pending    → henüz fiyat entry zone'a girmedi
  entered    → fiyat entry zone'a girdi (giriş fırsatı gerçekleşti)
  target_hit → TP'ye ulaştı
  stopped    → SL tetiklendi
  expired    → gün içinde hiçbir şey olmadı, seans bitti
  cancelled  → bir sonraki tarama sinyali iptal etti

Amaç: "keşke" sinyali yerine **retrospektif öğrenme** ve **gerçek isabet oranı**.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    from config import DATA_DIR as _DATA_DIR
except Exception:
    _DATA_DIR = str(Path(__file__).parent)

_DB_PATH = Path(_DATA_DIR) / "signal_history.db"


# ──────────────────────────────────────────────────────────────────
# Schema
# ──────────────────────────────────────────────────────────────────

def init_db() -> None:
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id        TEXT NOT NULL,
                scan_time      TEXT NOT NULL,
                date_tr        TEXT NOT NULL,
                ticker         TEXT NOT NULL,
                action         TEXT NOT NULL,
                confidence     INTEGER,
                strategy       TEXT,
                regime         TEXT,
                entry_low      REAL,
                entry_high     REAL,
                stop_loss      REAL,
                take_profit    REAL,
                risk_reward    TEXT,
                position_pct   REAL,
                urgency        TEXT,
                reasoning      TEXT,
                risk_note      TEXT,
                price_at_scan  REAL,
                data_age_min   INTEGER,
                hit_status     TEXT DEFAULT 'pending',
                hit_time       TEXT,
                hit_price      REAL,
                outcome_pct    REAL,
                expected_exit  TEXT,
                max_hold_min   INTEGER,
                raw_json       TEXT
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_date ON signals(date_tr)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_ticker ON signals(ticker)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_status ON signals(hit_status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_scan ON signals(scan_id)")
        conn.commit()


# ──────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────

def _parse_entry_zone(zone: str | None) -> tuple[Optional[float], Optional[float]]:
    """'210.50-212.00' → (210.50, 212.00). '₺42,50 - ₺43,00' → (42.5, 43.0).
    Fiyatlar pozitif olduğu için negatif işareti aranmaz — "-" zaten range ayracı."""
    if not zone:
        return (None, None)
    if isinstance(zone, (int, float)):
        v = float(zone)
        return (v, v)
    s = str(zone).replace(",", ".").replace("₺", "").strip()
    nums = re.findall(r"\d+(?:\.\d+)?", s)
    if not nums:
        return (None, None)
    if len(nums) == 1:
        v = float(nums[0])
        return (v, v)
    a, b = float(nums[0]), float(nums[1])
    return (min(a, b), max(a, b))


def _to_float(v) -> Optional[float]:
    """Fiyat alanları için pozitif float parser (% değişim gibi negatif sayılar için kullanılmaz)."""
    if v is None or v == "":
        return None
    try:
        if isinstance(v, str):
            v = v.replace(",", ".").replace("₺", "").strip()
            nums = re.findall(r"\d+(?:\.\d+)?", v)
            if not nums:
                return None
            return float(nums[0])
        return float(v)
    except Exception:
        return None


def _date_tr(iso_ts: str | None) -> str:
    try:
        if not iso_ts:
            return datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return dt.astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ──────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────

def log_scan(scan_result: dict, market_data: dict | None = None) -> int:
    """
    Bir run_scan sonucunu logla. Her karar için bir satır yazılır.

    Returns:
        Yazılan sinyal sayısı.
    """
    if not scan_result:
        return 0

    decisions = scan_result.get("decisions") or []
    if not decisions:
        return 0

    scan_time = scan_result.get("timestamp") or datetime.now(timezone.utc).isoformat()
    scan_id = f"scan_{scan_time}"
    date_tr = _date_tr(scan_time)
    regime = scan_result.get("regime", "unknown")
    active_strategy = scan_result.get("active_strategy", "")

    # Veri tazelik: market_data içindeki en eski timestamp ile scan_time arasındaki fark
    data_age_min = _compute_data_age(scan_time, market_data)

    written = 0
    with sqlite3.connect(_DB_PATH) as conn:
        for d in decisions:
            ticker = d.get("ticker") or d.get("symbol") or ""
            if not ticker:
                continue

            entry_low, entry_high = _parse_entry_zone(d.get("entry_zone") or d.get("entry"))
            price_at_scan = None
            if market_data and isinstance(market_data, dict):
                md = market_data.get(ticker)
                if isinstance(md, dict):
                    price_at_scan = _to_float(md.get("price"))

            conn.execute(
                """INSERT INTO signals
                (scan_id, scan_time, date_tr, ticker, action, confidence, strategy, regime,
                 entry_low, entry_high, stop_loss, take_profit, risk_reward, position_pct,
                 urgency, reasoning, risk_note, price_at_scan, data_age_min,
                 expected_exit, max_hold_min, raw_json)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    scan_id, scan_time, date_tr, ticker,
                    d.get("action", "watch"),
                    int(d.get("confidence") or 0),
                    d.get("strategy") or active_strategy,
                    regime,
                    entry_low, entry_high,
                    _to_float(d.get("stop_loss")),
                    _to_float(d.get("take_profit")),
                    d.get("risk_reward") or "",
                    _to_float(d.get("position_size_pct")),
                    d.get("urgency") or "",
                    (d.get("reasoning") or "")[:4000],
                    (d.get("risk_note") or "")[:1000],
                    price_at_scan,
                    data_age_min,
                    d.get("expected_exit") or "",
                    int(d.get("max_hold_minutes") or 0),
                    json.dumps(d, ensure_ascii=False)[:16000],
                ),
            )
            written += 1

        # Eski pending sinyalleri "cancelled" olarak işaretle:
        # aynı ticker + aynı gün + yeni scan geldi = eski artık geçerli değil
        conn.execute(
            """UPDATE signals
               SET hit_status='cancelled'
               WHERE hit_status='pending'
                 AND date_tr=?
                 AND scan_id != ?
                 AND ticker IN (SELECT ticker FROM signals WHERE scan_id=?)""",
            (date_tr, scan_id, scan_id),
        )
        conn.commit()

    return written


def _compute_data_age(scan_time_iso: str, market_data: dict | None) -> int:
    """Scan anındaki veri yaşı (dakika). market_data'daki timestamp'e göre."""
    if not market_data:
        return 0
    try:
        scan_dt = datetime.fromisoformat(scan_time_iso.replace("Z", "+00:00"))
    except Exception:
        return 0

    newest: Optional[datetime] = None
    for v in market_data.values():
        if not isinstance(v, dict):
            continue
        ts = v.get("last_updated") or v.get("timestamp") or v.get("as_of")
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            if newest is None or dt > newest:
                newest = dt
        except Exception:
            continue
    if newest is None:
        return 0
    delta = scan_dt - newest
    return max(0, int(delta.total_seconds() // 60))


# ──────────────────────────────────────────────────────────────────
# Hit tracking (scheduler'dan çağrılır)
# ──────────────────────────────────────────────────────────────────

def update_hit_status(live_prices: dict) -> dict:
    """
    Bekleyen (pending) ve giriş yapılmış (entered) sinyalleri canlı fiyatla
    karşılaştır, hit_status'u güncelle.

    Args:
        live_prices: {"GARAN.IS": 42.85, ...} — en güncel fiyatlar
    Returns:
        {
          "updated": int,
          "newly_approaching": [...],  # Timing Agent: entry zone'a %1 yaklaştı
          "newly_entered": [...],      # Entry zone'a girildi
          "tp_hits": [...],
          "sl_hits": [...],
        }
    """
    if not live_prices:
        return {"updated": 0, "newly_approaching": [], "newly_entered": [], "tp_hits": [], "sl_hits": []}

    now_iso = datetime.now(timezone.utc).isoformat()
    newly_approaching: list[dict] = []
    newly_entered: list[dict] = []
    tp_hits: list[dict] = []
    sl_hits: list[dict] = []
    updated = 0

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT id, ticker, action, entry_low, entry_high, stop_loss, take_profit,
                      price_at_scan, confidence, hit_status
               FROM signals
               WHERE hit_status IN ('pending','approaching','entered')
                 AND action IN ('long','close_long','short')
                 AND date_tr = ?""",
            (datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d"),),
        ).fetchall()

        for r in rows:
            px = live_prices.get(r["ticker"])
            if px is None:
                continue
            try:
                price = float(px)
            except Exception:
                continue

            new_status = r["hit_status"]
            hit_price = None
            outcome_pct = None

            # Long sinyal
            if r["action"] == "long":
                entry_low = r["entry_low"]
                entry_high = r["entry_high"]
                sl = r["stop_loss"]
                tp = r["take_profit"]

                if r["hit_status"] in ("pending", "approaching"):
                    # Fiyat entry zone içine girdi mi?
                    if entry_low is not None and entry_high is not None:
                        if price <= entry_high * 1.001:  # küçük tolerans
                            new_status = "entered"
                            hit_price = price
                            newly_entered.append({"ticker": r["ticker"], "price": price, "confidence": r["confidence"]})
                        elif r["hit_status"] == "pending" and price <= entry_high * 1.012:
                            # %1.2'ye kadar yaklaşmış — Timing Agent uyarısı
                            new_status = "approaching"
                            distance_pct = (price - entry_high) / entry_high * 100
                            newly_approaching.append({
                                "ticker": r["ticker"],
                                "price": price,
                                "entry_high": entry_high,
                                "distance_pct": round(distance_pct, 2),
                                "confidence": r["confidence"],
                            })

                if new_status in ("entered",) or r["hit_status"] == "entered":
                    # Entry sonrası TP/SL kontrolü
                    ref_price = hit_price or r["price_at_scan"] or price
                    if sl is not None and price <= sl:
                        new_status = "stopped"
                        hit_price = price
                        outcome_pct = ((price - ref_price) / ref_price * 100) if ref_price else 0
                        sl_hits.append({"ticker": r["ticker"], "price": price, "pct": outcome_pct})
                    elif tp is not None and price >= tp:
                        new_status = "target_hit"
                        hit_price = price
                        outcome_pct = ((price - ref_price) / ref_price * 100) if ref_price else 0
                        tp_hits.append({"ticker": r["ticker"], "price": price, "pct": outcome_pct})

            if new_status != r["hit_status"]:
                conn.execute(
                    """UPDATE signals SET hit_status=?, hit_time=?, hit_price=COALESCE(?, hit_price),
                                         outcome_pct=COALESCE(?, outcome_pct)
                       WHERE id=?""",
                    (new_status, now_iso, hit_price, outcome_pct, r["id"]),
                )
                updated += 1
        conn.commit()

    return {
        "updated": updated,
        "newly_approaching": newly_approaching,
        "newly_entered": newly_entered,
        "tp_hits": tp_hits,
        "sl_hits": sl_hits,
    }


def mark_expired_at_eod() -> int:
    """Seans sonu: hâlâ pending ya da entered ama TP/SL dokunmamış sinyalleri 'expired'."""
    today_tr = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
    with sqlite3.connect(_DB_PATH) as conn:
        cur = conn.execute(
            """UPDATE signals SET hit_status='expired',
                                  hit_time=?
               WHERE hit_status IN ('pending','entered')
                 AND date_tr=?""",
            (datetime.now(timezone.utc).isoformat(), today_tr),
        )
        conn.commit()
        return cur.rowcount


# ──────────────────────────────────────────────────────────────────
# Query API
# ──────────────────────────────────────────────────────────────────

def get_signals(
    ticker: str | None = None,
    date: str | None = None,
    status: str | None = None,
    limit: int = 200,
) -> list[dict]:
    where = []
    params: list = []
    if ticker:
        where.append("ticker=?")
        params.append(ticker)
    if date:
        where.append("date_tr=?")
        params.append(date)
    if status:
        where.append("hit_status=?")
        params.append(status)
    sql = "SELECT * FROM signals"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY scan_time DESC LIMIT ?"
    params.append(limit)

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_timeline(date: str | None = None) -> list[dict]:
    """Verilen günün (default: bugün TR) tüm sinyallerini zaman sırasıyla."""
    if not date:
        date = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
    return list(reversed(get_signals(date=date, limit=1000)))


def get_performance(days: int = 30) -> dict:
    """Sinyal isabet istatistikleri."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT hit_status, COUNT(*) AS n, AVG(outcome_pct) AS avg_pct
               FROM signals
               WHERE date_tr >= ? AND action='long'
               GROUP BY hit_status""",
            (since,),
        ).fetchall()

    stats = {r["hit_status"]: {"count": r["n"], "avg_pct": r["avg_pct"] or 0} for r in rows}
    total = sum(s["count"] for s in stats.values())
    entered = stats.get("entered", {}).get("count", 0) + stats.get("target_hit", {}).get("count", 0) + stats.get("stopped", {}).get("count", 0)
    tp_hits = stats.get("target_hit", {}).get("count", 0)
    sl_hits = stats.get("stopped", {}).get("count", 0)

    # Kalite skorları
    entry_rate = (entered / total * 100) if total else 0
    hit_rate = (tp_hits / entered * 100) if entered else 0
    avg_winner = stats.get("target_hit", {}).get("avg_pct", 0) or 0
    avg_loser = stats.get("stopped", {}).get("avg_pct", 0) or 0

    return {
        "period_days": days,
        "total_signals": total,
        "entry_rate_pct": round(entry_rate, 1),
        "hit_rate_pct": round(hit_rate, 1),
        "target_hits": tp_hits,
        "stop_hits": sl_hits,
        "avg_winner_pct": round(avg_winner, 2),
        "avg_loser_pct": round(avg_loser, 2),
        "by_status": stats,
    }


def get_active_signals() -> list[dict]:
    """Bugün üretilen ve henüz kapanmamış (pending veya entered) sinyaller."""
    today_tr = datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d")
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT * FROM signals
               WHERE date_tr=? AND hit_status IN ('pending','entered')
               ORDER BY confidence DESC, scan_time DESC""",
            (today_tr,),
        ).fetchall()
        return [dict(r) for r in rows]
