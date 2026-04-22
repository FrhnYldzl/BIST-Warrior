"""
midas_journal.py — Midas Dilinde İşlem Günlüğü (Meridian TR V2)

Midas'ın kendi dili + Meridian AI analist dili aynı yerde.

Midas'ta destekleyen 5 emir tipi:
  1. Piyasa Emri         (piyasa)          → Anında en iyi fiyattan
  2. Limit Emri          (limit)           → Belirlenen fiyat veya daha iyisinden
  3. Stop Emri           (stop)            → Stop fiyatına ulaşınca piyasa emri tetiklenir
  4. Stop-Limit Emri     (stop_limit)      → Stop fiyatına ulaşınca limit emri tetiklenir
  5. Kâr Al / Zarar Dur  (bracket)         → Ana emir + TP + SL birlikte

Emir Süresi:
  - GUN (günlük): Gün sonunda pasife düşer
  - IEK (İptale Kadar Geçerli): Manuel iptal edilene kadar

Kullanıcı Midas'ta emir verdikten sonra buraya kaydeder.
Sistem AI sinyali ile gerçek emri karşılaştırır, performansı takip eder.
"""

import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

try:
    from config import DATA_DIR as _DATA_DIR
except Exception:
    _DATA_DIR = str(Path(__file__).parent)
_DB_PATH = Path(_DATA_DIR) / "midas_journal.db"


# ─── Midas Emir Tipleri (UI için) ─────────────────────────────────
ORDER_TYPES = {
    "piyasa": {
        "label": "Piyasa Emri",
        "desc": "Anında en iyi fiyattan al/sat",
        "fields": ["qty"],
        "midas_label": "Piyasa",
        "risk": "Hızlı dolar ama fiyat sapması riski",
    },
    "limit": {
        "label": "Limit Emri",
        "desc": "Belirlenen fiyattan veya daha iyisinden",
        "fields": ["qty", "limit_price"],
        "midas_label": "Limit",
        "risk": "Fiyat gelmezse emir pasif kalır",
    },
    "stop": {
        "label": "Stop Emri",
        "desc": "Stop fiyatı görülünce piyasa emri tetiklenir",
        "fields": ["qty", "stop_price"],
        "midas_label": "Stop",
        "risk": "Tetiklenince piyasa fiyatından dolar (sapabilir)",
    },
    "stop_limit": {
        "label": "Stop-Limit Emri",
        "desc": "Stop fiyatı görülünce belirlenen limit emri tetiklenir",
        "fields": ["qty", "stop_price", "limit_price"],
        "midas_label": "Stop-Limit",
        "risk": "Stop tetiklenince limit fiyat gelmezse dolmaz",
    },
    "bracket": {
        "label": "Kâr Al / Zarar Durdur",
        "desc": "Giriş emri + otomatik kâr al + zarar durdur",
        "fields": ["qty", "entry_price", "take_profit", "stop_loss"],
        "midas_label": "Kâr Al, Zarar Durdur",
        "risk": "En güvenli — hem kâr hem zarar otomatik yönetilir",
    },
}

TIME_IN_FORCE = {
    "GUN": {"label": "Günlük (GÜN)", "desc": "Seans sonunda iptal olur"},
    "IEK": {"label": "İptale Kadar Geçerli (İEK)", "desc": "Manuel iptal edilene kadar geçerli"},
}


def init_midas_db():
    """Tabloları oluştur — idempotent, mevcut DB'ye yeni kolonları ekler."""
    with sqlite3.connect(_DB_PATH) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS midas_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                action TEXT NOT NULL,              -- 'long' (alım) | 'short' (BIST yasak ama esneklik)
                order_type TEXT NOT NULL,          -- 'piyasa' | 'limit' | 'stop' | 'stop_limit' | 'bracket'
                time_in_force TEXT DEFAULT 'GUN',  -- 'GUN' | 'IEK'
                qty REAL NOT NULL,

                -- Fiyat alanları (emir tipine göre dolar)
                entry_price REAL,                  -- Gerçekleşen giriş (ne fiyattan dolduysan)
                limit_price REAL,                  -- Limit emri için
                stop_price REAL,                   -- Stop / Stop-Limit için
                take_profit REAL,                  -- Bracket için kâr al
                stop_loss REAL,                    -- Bracket için zarar durdur

                -- Çıkış
                exit_price REAL,
                exit_order_type TEXT,              -- Çıkışta kullandığın emir tipi
                exit_time TEXT,

                entry_time TEXT NOT NULL,
                pnl_try REAL,
                pnl_pct REAL,

                -- AI bağlantısı
                ai_signal_source TEXT,             -- Hangi Claude sinyalinden aldın
                ai_confidence INTEGER,
                ai_reasoning TEXT,                 -- Claude'un önerdiği gerekçe

                -- Kullanıcı
                midas_order_id TEXT,               -- Midas'tan aldığın emir # (opsiyonel)
                notes TEXT,
                status TEXT DEFAULT 'open',        -- 'pending' | 'open' | 'closed' | 'cancelled'
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_midas_ticker ON midas_trades(ticker);
            CREATE INDEX IF NOT EXISTS idx_midas_status ON midas_trades(status);
            CREATE INDEX IF NOT EXISTS idx_midas_entry ON midas_trades(entry_time);

            CREATE TABLE IF NOT EXISTS midas_config (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                starting_capital_try REAL DEFAULT 1000000,
                daily_goal_pct REAL DEFAULT 0.5,
                daily_max_loss_pct REAL DEFAULT 2.0,
                weekly_goal_pct REAL DEFAULT 2.5,
                monthly_goal_pct REAL DEFAULT 8.0,
                -- Kişisel kriterler
                min_trade_value_try REAL DEFAULT 500,          -- Altındaki önerileri filtrele
                max_trade_value_try REAL DEFAULT 100000,       -- Üstündeki önerilere uyarı
                min_ai_confidence INTEGER DEFAULT 6,           -- Altındaki AI sinyallerini görmek istemem
                margin_enabled INTEGER DEFAULT 0,              -- Kredili işlem açık mı (0/1)
                margin_min_confidence INTEGER DEFAULT 9,       -- Kredili için min güven
                margin_max_position_pct REAL DEFAULT 10,       -- Kredili için max pozisyon %
                margin_sectors TEXT DEFAULT ''                 -- CSV: "Bankacilik,Holding" — kredili sadece bu sektörlerde
            );
        """)
        # Var olan DB'ye eksik kolonları ekle (migration)
        _migrate_columns(conn)

        row = conn.execute("SELECT id FROM midas_config WHERE id=1").fetchone()
        if not row:
            conn.execute("INSERT INTO midas_config (id) VALUES (1)")
        conn.commit()


def _migrate_columns(conn):
    """Eski DB'lere yeni kolonları ekle (idempotent)."""
    new_cols = [
        ("order_type", "TEXT DEFAULT 'piyasa'"),
        ("time_in_force", "TEXT DEFAULT 'GUN'"),
        ("limit_price", "REAL"),
        ("stop_price", "REAL"),
        ("exit_order_type", "TEXT"),
        ("ai_reasoning", "TEXT"),
        ("midas_order_id", "TEXT"),
        ("is_margin", "INTEGER DEFAULT 0"),    # Kredili işlem mi?
    ]
    existing = [r[1] for r in conn.execute("PRAGMA table_info(midas_trades)").fetchall()]
    for name, defn in new_cols:
        if name not in existing:
            try:
                conn.execute(f"ALTER TABLE midas_trades ADD COLUMN {name} {defn}")
            except sqlite3.OperationalError:
                pass

    # Config yeni kolonlar
    cfg_new_cols = [
        ("min_trade_value_try", "REAL DEFAULT 500"),
        ("max_trade_value_try", "REAL DEFAULT 100000"),
        ("min_ai_confidence", "INTEGER DEFAULT 6"),
        ("margin_enabled", "INTEGER DEFAULT 0"),
        ("margin_min_confidence", "INTEGER DEFAULT 9"),
        ("margin_max_position_pct", "REAL DEFAULT 10"),
        ("margin_sectors", "TEXT DEFAULT ''"),
    ]
    cfg_existing = [r[1] for r in conn.execute("PRAGMA table_info(midas_config)").fetchall()]
    for name, defn in cfg_new_cols:
        if name not in cfg_existing:
            try:
                conn.execute(f"ALTER TABLE midas_config ADD COLUMN {name} {defn}")
            except sqlite3.OperationalError:
                pass


def log_trade(
    ticker: str,
    action: str,
    order_type: str,
    qty: float,
    entry_price: float = None,
    limit_price: float = None,
    stop_price: float = None,
    take_profit: float = None,
    stop_loss: float = None,
    time_in_force: str = "GUN",
    status: str = "open",                  # 'pending' (emir girildi ama dolmadı) | 'open' (dolu)
    ai_signal_source: str = "",
    ai_confidence: int = 0,
    ai_reasoning: str = "",
    midas_order_id: str = "",
    is_margin: int = 0,                    # 0 = nakit, 1 = kredili işlem
    notes: str = "",
) -> dict:
    """
    Midas'ta verdiğin emri kaydet.

    Emir tipine göre alanlar:
      - piyasa:      qty, entry_price (gerçekleşen)
      - limit:       qty, limit_price (+ entry_price dolunca)
      - stop:        qty, stop_price (+ entry_price tetiklenince)
      - stop_limit:  qty, stop_price, limit_price (+ entry_price dolunca)
      - bracket:     qty, entry_price, take_profit, stop_loss

    status='pending' → Midas'ta girdim ama henüz dolmadı (Limit bekliyor örn.)
    status='open'    → Dolu, açık pozisyon
    """
    ticker = (ticker or "").upper().strip()
    if not ticker.endswith(".IS") and "." not in ticker:
        ticker = ticker + ".IS"

    action = (action or "long").lower()
    order_type = (order_type or "piyasa").lower()

    if order_type not in ORDER_TYPES:
        return {"status": "error", "message": f"Geçersiz emir tipi: {order_type}"}

    now_iso = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(_DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO midas_trades
                (ticker, action, order_type, time_in_force, qty,
                 entry_price, limit_price, stop_price, take_profit, stop_loss,
                 entry_time, ai_signal_source, ai_confidence, ai_reasoning,
                 midas_order_id, is_margin, notes, status, created_at)
            VALUES (?,?,?,?,?, ?,?,?,?,?, ?,?,?,?, ?,?,?,?,?)
        """, (ticker, action, order_type, time_in_force, qty,
              entry_price, limit_price, stop_price, take_profit, stop_loss,
              now_iso, ai_signal_source, ai_confidence, ai_reasoning,
              midas_order_id, is_margin, notes, status, now_iso))
        trade_id = cur.lastrowid
        conn.commit()

    return {
        "status": "ok",
        "id": trade_id,
        "ticker": ticker,
        "action": action,
        "order_type": order_type,
        "order_type_label": ORDER_TYPES[order_type]["label"],
        "qty": qty,
        "message": f"Kaydedildi: {ticker} · {ORDER_TYPES[order_type]['label']} × {qty}",
    }


def mark_as_open(trade_id: int, fill_price: float) -> dict:
    """Pending emir doldu — gerçekleşen fiyatı gir."""
    now_iso = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("""
            UPDATE midas_trades
            SET entry_price=?, status='open', entry_time=?
            WHERE id=? AND status='pending'
        """, (fill_price, now_iso, trade_id))
        conn.commit()
    return {"status": "ok", "id": trade_id, "fill_price": fill_price}


def cancel_trade(trade_id: int, reason: str = "") -> dict:
    """Pending emri iptal et (Midas'ta da iptal ettin)."""
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        trade = conn.execute("SELECT * FROM midas_trades WHERE id=? AND status='pending'",
                             (trade_id,)).fetchone()
        if not trade:
            return {"status": "error", "message": "Pending emir bulunamadı"}
        conn.execute("""
            UPDATE midas_trades
            SET status='cancelled', notes = COALESCE(notes,'') || ?
            WHERE id=?
        """, (f"\n[İPTAL] {reason}" if reason else "\n[İPTAL]", trade_id))
        conn.commit()
    return {"status": "ok", "id": trade_id}


def close_trade(
    trade_id: int,
    exit_price: float,
    exit_order_type: str = "piyasa",
    notes: str = "",
) -> dict:
    """
    Açık pozisyonu kapat (Midas'ta satış yaptın).
    exit_order_type: kullanıcının çıkışta ne tip emir verdiği (piyasa/limit/stop…)
    """
    now_iso = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        trade = conn.execute("SELECT * FROM midas_trades WHERE id=? AND status='open'",
                             (trade_id,)).fetchone()
        if not trade:
            return {"status": "error", "message": f"#{trade_id} açık pozisyon bulunamadı"}

        qty = trade["qty"]
        entry = trade["entry_price"] or 0
        action = trade["action"]

        if action == "long":
            pnl = (exit_price - entry) * qty
        else:
            pnl = (entry - exit_price) * qty
        pnl_pct = (pnl / (entry * qty)) * 100 if entry > 0 and qty > 0 else 0

        existing_notes = trade["notes"] or ""
        final_notes = existing_notes + (f"\n[KAPAT] {notes}" if notes else "")

        conn.execute("""
            UPDATE midas_trades
            SET exit_price=?, exit_time=?, exit_order_type=?,
                pnl_try=?, pnl_pct=?, notes=?, status='closed'
            WHERE id=?
        """, (exit_price, now_iso, exit_order_type, pnl, pnl_pct, final_notes, trade_id))
        conn.commit()

    return {
        "status": "ok",
        "id": trade_id,
        "ticker": trade["ticker"],
        "entry_price": entry,
        "exit_price": exit_price,
        "exit_order_type": exit_order_type,
        "qty": qty,
        "pnl_try": round(pnl, 2),
        "pnl_pct": round(pnl_pct, 2),
        "message": f"Kapatıldı: {trade['ticker']} · K/Z: {'+' if pnl >= 0 else ''}{pnl:.2f} ₺ ({pnl_pct:+.2f}%)",
    }


def get_positions(status: str = "open") -> list:
    """
    Pozisyonları listele.
    status: 'open' | 'pending' | 'closed' | 'all'
    """
    query = "SELECT * FROM midas_trades"
    params = []
    if status != "all":
        query += " WHERE status=?"
        params.append(status)
    query += " ORDER BY entry_time DESC"

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_trade_history(limit: int = 50, ticker: str = None, days: int = None) -> list:
    """Kapalı işlem geçmişi."""
    query = "SELECT * FROM midas_trades WHERE status='closed'"
    params = []

    if ticker:
        query += " AND ticker=?"
        params.append(ticker.upper())

    if days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        query += " AND entry_time >= ?"
        params.append(cutoff)

    query += " ORDER BY exit_time DESC LIMIT ?"
    params.append(limit)

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def get_daily_stats() -> dict:
    """Bugünün özeti — K/Z, hedef durumu, risk seviyesi."""
    now_tr = datetime.now(timezone.utc) + timedelta(hours=3)
    today_start = now_tr.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = (today_start - timedelta(hours=3)).isoformat()

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cfg = conn.execute("SELECT * FROM midas_config WHERE id=1").fetchone()
        if not cfg:
            return {"error": "Config bulunamadı"}
        cfg = dict(cfg)
        starting_capital = cfg["starting_capital_try"]

        closed_today = conn.execute("""
            SELECT * FROM midas_trades
            WHERE status='closed' AND exit_time >= ?
        """, (today_start_utc,)).fetchall()

        opened_today = conn.execute("""
            SELECT * FROM midas_trades
            WHERE entry_time >= ?
        """, (today_start_utc,)).fetchall()

        realized_pnl = sum(r["pnl_try"] or 0 for r in closed_today)
        realized_pnl_pct = (realized_pnl / starting_capital * 100) if starting_capital > 0 else 0

        goal_try = starting_capital * cfg["daily_goal_pct"] / 100
        max_loss_try = -abs(starting_capital * cfg["daily_max_loss_pct"] / 100)
        goal_reached = realized_pnl >= goal_try
        stop_triggered = realized_pnl <= max_loss_try

        n_closed = len(closed_today)
        n_winners = sum(1 for r in closed_today if (r["pnl_try"] or 0) > 0)
        n_losers = sum(1 for r in closed_today if (r["pnl_try"] or 0) < 0)
        win_rate = (n_winners / n_closed * 100) if n_closed > 0 else 0

        n_open = conn.execute("SELECT COUNT(*) FROM midas_trades WHERE status='open'").fetchone()[0]
        n_pending = conn.execute("SELECT COUNT(*) FROM midas_trades WHERE status='pending'").fetchone()[0]

    return {
        "date": now_tr.strftime("%Y-%m-%d"),
        "starting_capital_try": starting_capital,
        "daily_goal_pct": cfg["daily_goal_pct"],
        "daily_max_loss_pct": cfg["daily_max_loss_pct"],
        "goal_try": round(goal_try, 2),
        "max_loss_try": round(max_loss_try, 2),
        "realized_pnl_try": round(realized_pnl, 2),
        "realized_pnl_pct": round(realized_pnl_pct, 2),
        "goal_reached": goal_reached,
        "stop_triggered": stop_triggered,
        "progress_pct": round((realized_pnl / goal_try * 100) if goal_try > 0 else 0, 1),
        "n_trades_today": n_closed,
        "n_opened_today": len(opened_today),
        "n_open_now": n_open,
        "n_pending": n_pending,
        "n_winners": n_winners,
        "n_losers": n_losers,
        "win_rate_pct": round(win_rate, 1),
    }


def get_performance_summary(days: int = 30) -> dict:
    """Son N gün performans özeti — AI'a karşı gerçek performans."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cfg = conn.execute("SELECT * FROM midas_config WHERE id=1").fetchone()
        starting_capital = cfg["starting_capital_try"] if cfg else 1000000

        rows = conn.execute("""
            SELECT * FROM midas_trades
            WHERE status='closed' AND exit_time >= ?
            ORDER BY exit_time DESC
        """, (cutoff,)).fetchall()
        trades = [dict(r) for r in rows]

    if not trades:
        return {
            "days": days, "n_trades": 0, "total_pnl_try": 0, "total_pnl_pct": 0,
            "win_rate_pct": 0, "avg_pnl_pct": 0, "best_trade": None, "worst_trade": None,
            "ai_follow_rate_pct": 0, "by_order_type": {},
        }

    total_pnl = sum(t.get("pnl_try", 0) or 0 for t in trades)
    total_pnl_pct = (total_pnl / starting_capital * 100) if starting_capital > 0 else 0
    winners = [t for t in trades if (t.get("pnl_try") or 0) > 0]
    losers = [t for t in trades if (t.get("pnl_try") or 0) < 0]
    win_rate = (len(winners) / len(trades) * 100) if trades else 0
    avg_pnl_pct = sum(t.get("pnl_pct", 0) or 0 for t in trades) / len(trades)

    best = max(trades, key=lambda t: t.get("pnl_pct") or -999)
    worst = min(trades, key=lambda t: t.get("pnl_pct") or 999)

    ai_followed = [t for t in trades if (t.get("ai_signal_source") or "").strip()]
    ai_follow_rate = (len(ai_followed) / len(trades) * 100) if trades else 0

    # Emir tipine göre performans
    by_type = {}
    for t in trades:
        ot = t.get("order_type") or "piyasa"
        if ot not in by_type:
            by_type[ot] = {"count": 0, "winners": 0, "total_pnl": 0}
        by_type[ot]["count"] += 1
        if (t.get("pnl_try") or 0) > 0:
            by_type[ot]["winners"] += 1
        by_type[ot]["total_pnl"] += t.get("pnl_try") or 0
    for ot in by_type:
        c = by_type[ot]["count"]
        by_type[ot]["win_rate_pct"] = round(by_type[ot]["winners"] / c * 100, 1) if c > 0 else 0
        by_type[ot]["total_pnl"] = round(by_type[ot]["total_pnl"], 2)
        by_type[ot]["label"] = ORDER_TYPES.get(ot, {}).get("label", ot)

    return {
        "days": days,
        "n_trades": len(trades),
        "n_winners": len(winners),
        "n_losers": len(losers),
        "total_pnl_try": round(total_pnl, 2),
        "total_pnl_pct": round(total_pnl_pct, 2),
        "win_rate_pct": round(win_rate, 1),
        "avg_pnl_pct": round(avg_pnl_pct, 2),
        "best_trade": {
            "ticker": best["ticker"],
            "pnl_pct": round(best.get("pnl_pct") or 0, 2),
            "pnl_try": round(best.get("pnl_try") or 0, 2),
        },
        "worst_trade": {
            "ticker": worst["ticker"],
            "pnl_pct": round(worst.get("pnl_pct") or 0, 2),
            "pnl_try": round(worst.get("pnl_try") or 0, 2),
        },
        "ai_follow_rate_pct": round(ai_follow_rate, 1),
        "by_order_type": by_type,
    }


def update_config(**kwargs) -> dict:
    """Hedef/sermaye + kişisel kriter ayarlarını güncelle."""
    allowed = ("starting_capital_try", "daily_goal_pct", "daily_max_loss_pct",
               "weekly_goal_pct", "monthly_goal_pct",
               "min_trade_value_try", "max_trade_value_try", "min_ai_confidence",
               "margin_enabled", "margin_min_confidence", "margin_max_position_pct",
               "margin_sectors")
    fields = []
    params = []
    for name, val in kwargs.items():
        if name in allowed and val is not None:
            fields.append(f"{name}=?")
            params.append(val)

    if not fields:
        return {"status": "error", "message": "Güncellenecek alan yok"}

    params.append(1)
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(f"UPDATE midas_config SET {', '.join(fields)} WHERE id=?", params)
        conn.commit()
    return get_config()


def get_config() -> dict:
    """Mevcut config."""
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM midas_config WHERE id=1").fetchone()
    return dict(row) if row else {}


def delete_trade(trade_id: int) -> dict:
    """İşlem kaydını sil."""
    with sqlite3.connect(_DB_PATH) as conn:
        cur = conn.execute("DELETE FROM midas_trades WHERE id=?", (trade_id,))
        deleted = cur.rowcount
        conn.commit()
    return {
        "status": "ok" if deleted else "error",
        "deleted": deleted,
        "message": f"#{trade_id} silindi" if deleted else f"#{trade_id} bulunamadı",
    }


def get_order_types_info() -> dict:
    """Dashboard dropdown'ı için emir tipi bilgileri."""
    return {
        "order_types": ORDER_TYPES,
        "time_in_force": TIME_IN_FORCE,
    }


# ═══════════════════════════════════════════════════════════════
# 🌅 Daily Plan Cache — AI'nın gün başı önerisi + kullanıcı onayı
# ═══════════════════════════════════════════════════════════════

def init_daily_plan_table():
    """Günlük plan tablosu — her gün için AI önerisi + onay durumu."""
    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS daily_plans (
                date TEXT PRIMARY KEY,
                day_quality TEXT,
                daily_budget_try REAL,
                daily_profit_target_try REAL,
                max_trades INTEGER,
                credit_enabled INTEGER DEFAULT 0,
                reasoning TEXT,
                key_risks TEXT,        -- JSON array
                focus_sectors TEXT,    -- JSON array
                avoid_sectors TEXT,    -- JSON array
                market_outlook TEXT,
                confidence_in_plan INTEGER,
                user_status TEXT DEFAULT 'pending',   -- 'pending' | 'approved' | 'adjusted' | 'skipped'
                user_notes TEXT,
                ai_raw TEXT,           -- AI'nın ham çıktısı (JSON)
                created_at TEXT
            )
        """)
        conn.commit()


def save_daily_plan(plan: dict) -> dict:
    """AI briefing'i DB'ye yaz (bugünün planı)."""
    import json as _json
    init_daily_plan_table()
    now_tr = datetime.now(timezone.utc) + timedelta(hours=3)
    date = now_tr.strftime("%Y-%m-%d")

    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO daily_plans
              (date, day_quality, daily_budget_try, daily_profit_target_try,
               max_trades, credit_enabled, reasoning,
               key_risks, focus_sectors, avoid_sectors,
               market_outlook, confidence_in_plan, user_status,
               ai_raw, created_at)
            VALUES (?,?,?,?,?,?,?, ?,?,?, ?,?,?, ?,?)
        """, (
            date,
            plan.get("day_quality", "unknown"),
            plan.get("daily_budget_try", 0),
            plan.get("daily_profit_target_try", 0),
            plan.get("max_trades", 0),
            1 if plan.get("credit_enabled") else 0,
            plan.get("reasoning", ""),
            _json.dumps(plan.get("key_risks", [])),
            _json.dumps(plan.get("focus_sectors", [])),
            _json.dumps(plan.get("avoid_sectors", [])),
            plan.get("market_outlook", ""),
            plan.get("confidence_in_plan", 0),
            "pending",
            _json.dumps(plan),
            datetime.now(timezone.utc).isoformat(),
        ))
        conn.commit()
    return {"status": "ok", "date": date}


def get_daily_plan(date: str = None) -> dict:
    """Güncel / belirli tarihli plan."""
    import json as _json
    init_daily_plan_table()
    if not date:
        now_tr = datetime.now(timezone.utc) + timedelta(hours=3)
        date = now_tr.strftime("%Y-%m-%d")

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM daily_plans WHERE date=?", (date,)).fetchone()
    if not row:
        return {"date": date, "user_status": "not_generated"}

    d = dict(row)
    # JSON kolonlarını decode et
    for k in ("key_risks", "focus_sectors", "avoid_sectors"):
        try: d[k] = _json.loads(d[k] or "[]")
        except: d[k] = []
    d["credit_enabled"] = bool(d.get("credit_enabled"))
    return d


def compute_daily_report(date: str = None) -> dict:
    """
    Belirtilen günün raporu — hedef gerçekleşme + AI uyum + K/Z + açık pozisyonlar tahmini.
    Sistemde kalıcı yazılmaz, her sorguda hesaplanır (hesaplar hızlı).
    """
    import json as _json
    if not date:
        now_tr = datetime.now(timezone.utc) + timedelta(hours=3)
        date = now_tr.strftime("%Y-%m-%d")

    # Tarih aralığı (UTC)
    day_start = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    day_start_utc = (day_start - timedelta(hours=3)).isoformat()
    day_end_utc = (day_start + timedelta(hours=21)).isoformat()  # TRT sonu = UTC +21

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cfg = conn.execute("SELECT * FROM midas_config WHERE id=1").fetchone()
        starting_capital = cfg["starting_capital_try"] if cfg else 1000000

        # Planlar
        plan = conn.execute("SELECT * FROM daily_plans WHERE date=?", (date,)).fetchone()

        # Gün içinde kapatılan işlemler
        closed = conn.execute("""
            SELECT * FROM midas_trades
            WHERE status='closed' AND exit_time >= ? AND exit_time < ?
        """, (day_start_utc, day_end_utc)).fetchall()
        closed = [dict(r) for r in closed]

        # Gün içinde açılan (hala açık olabilir)
        opened = conn.execute("""
            SELECT * FROM midas_trades
            WHERE entry_time >= ? AND entry_time < ?
        """, (day_start_utc, day_end_utc)).fetchall()
        opened = [dict(r) for r in opened]

    realized_pnl = sum(t.get("pnl_try", 0) or 0 for t in closed)
    n_winners = sum(1 for t in closed if (t.get("pnl_try") or 0) > 0)
    n_losers = sum(1 for t in closed if (t.get("pnl_try") or 0) < 0)

    # AI uyum
    ai_opened = [t for t in opened if (t.get("ai_signal_source") or "").strip()]
    ai_follow_rate = (len(ai_opened) / len(opened) * 100) if opened else 0

    # Hedef karşılaştırma
    goal_try = plan["daily_profit_target_try"] if plan else 0
    budget_try = plan["daily_budget_try"] if plan else 0
    max_trades = plan["max_trades"] if plan else 0
    day_quality = plan["day_quality"] if plan else "unknown"
    plan_status = plan["user_status"] if plan else "none"

    goal_progress = (realized_pnl / goal_try * 100) if goal_try > 0 else 0
    goal_reached = realized_pnl >= goal_try if goal_try > 0 else False

    # Açık pozisyon kullanımı
    budget_used = sum((t.get("qty", 0) or 0) * (t.get("entry_price", 0) or 0) for t in opened)
    budget_used_pct = (budget_used / budget_try * 100) if budget_try > 0 else 0

    # En iyi / en kötü işlem
    best = max(closed, key=lambda t: t.get("pnl_pct") or -999, default=None)
    worst = min(closed, key=lambda t: t.get("pnl_pct") or 999, default=None)

    return {
        "date": date,
        "plan": {
            "day_quality": day_quality,
            "budget_try": budget_try,
            "goal_try": goal_try,
            "max_trades": max_trades,
            "status": plan_status,
        },
        "realized": {
            "pnl_try": round(realized_pnl, 2),
            "pnl_pct": round((realized_pnl / starting_capital * 100) if starting_capital > 0 else 0, 2),
            "n_closed": len(closed),
            "n_winners": n_winners,
            "n_losers": n_losers,
            "win_rate_pct": round((n_winners / len(closed) * 100) if closed else 0, 1),
        },
        "goal": {
            "reached": goal_reached,
            "progress_pct": round(goal_progress, 1),
            "remaining_try": round(goal_try - realized_pnl, 2) if goal_try > realized_pnl else 0,
        },
        "budget": {
            "used_try": round(budget_used, 2),
            "used_pct": round(budget_used_pct, 1),
            "remaining_try": round(max(0, budget_try - budget_used), 2),
        },
        "opened_today": {
            "count": len(opened),
            "still_open": sum(1 for t in opened if t.get("status") == "open"),
            "trades_limit_reached": len(opened) >= max_trades if max_trades > 0 else False,
        },
        "ai_compliance": {
            "followed_count": len(ai_opened),
            "total_trades": len(opened),
            "follow_rate_pct": round(ai_follow_rate, 1),
        },
        "best_trade": {
            "ticker": best["ticker"].replace(".IS", "") if best else None,
            "pnl_try": round(best.get("pnl_try") or 0, 2) if best else 0,
            "pnl_pct": round(best.get("pnl_pct") or 0, 2) if best else 0,
        } if best else None,
        "worst_trade": {
            "ticker": worst["ticker"].replace(".IS", "") if worst else None,
            "pnl_try": round(worst.get("pnl_try") or 0, 2) if worst else 0,
            "pnl_pct": round(worst.get("pnl_pct") or 0, 2) if worst else 0,
        } if worst else None,
    }


def get_calendar_heatmap(days: int = 30) -> list:
    """GitHub-style günlük K/Z heatmap verisi."""
    now_tr = datetime.now(timezone.utc) + timedelta(hours=3)
    result = []
    for i in range(days):
        d = (now_tr - timedelta(days=i)).strftime("%Y-%m-%d")
        day_start = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        day_start_utc = (day_start - timedelta(hours=3)).isoformat()
        day_end_utc = (day_start + timedelta(hours=21)).isoformat()
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute("""
                SELECT COALESCE(SUM(pnl_try),0) as pnl, COUNT(*) as n
                FROM midas_trades
                WHERE status='closed' AND exit_time >= ? AND exit_time < ?
            """, (day_start_utc, day_end_utc)).fetchone()
        result.append({
            "date": d,
            "pnl_try": round(row[0] or 0, 2),
            "n_trades": row[1] or 0,
            "weekday": day_start.weekday(),  # 0=Mon, 6=Sun
        })
    return list(reversed(result))  # Eski → yeni sıra


def get_cumulative_series(days: int = 90) -> dict:
    """
    Aylık grafik için kümülatif K/Z serisi.
    XU100 karşılaştırma şimdilik opsiyonel (yfinance'ten çekilir).
    """
    now_tr = datetime.now(timezone.utc) + timedelta(hours=3)
    cutoff = (now_tr - timedelta(days=days)).strftime("%Y-%m-%d")
    cutoff_utc = (datetime.strptime(cutoff, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(hours=3)).isoformat()

    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cfg = conn.execute("SELECT * FROM midas_config WHERE id=1").fetchone()
        starting_capital = cfg["starting_capital_try"] if cfg else 1000000

        rows = conn.execute("""
            SELECT DATE(exit_time, '+3 hours') as date,
                   SUM(pnl_try) as daily_pnl,
                   COUNT(*) as n_trades
            FROM midas_trades
            WHERE status='closed' AND exit_time >= ?
            GROUP BY DATE(exit_time, '+3 hours')
            ORDER BY date ASC
        """, (cutoff_utc,)).fetchall()

    # Günlük → kümülatif
    cumulative = 0
    series = []
    for r in rows:
        cumulative += r["daily_pnl"] or 0
        series.append({
            "date": r["date"],
            "daily_pnl": round(r["daily_pnl"] or 0, 2),
            "cumulative_pnl": round(cumulative, 2),
            "cumulative_pct": round((cumulative / starting_capital * 100) if starting_capital > 0 else 0, 2),
            "n_trades": r["n_trades"],
        })

    return {
        "starting_capital": starting_capital,
        "days": days,
        "total_pnl": round(cumulative, 2),
        "total_pct": round((cumulative / starting_capital * 100) if starting_capital > 0 else 0, 2),
        "series": series,
    }


def get_lifetime_stats() -> dict:
    """Tüm zamanlar — inception-to-date."""
    with sqlite3.connect(_DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cfg = conn.execute("SELECT * FROM midas_config WHERE id=1").fetchone()
        starting_capital = cfg["starting_capital_try"] if cfg else 1000000

        rows = conn.execute("SELECT * FROM midas_trades WHERE status='closed' ORDER BY exit_time ASC").fetchall()

    trades = [dict(r) for r in rows]

    if not trades:
        return {
            "starting_capital": starting_capital,
            "total_trades": 0,
            "total_pnl_try": 0,
            "total_pnl_pct": 0,
            "win_rate_pct": 0,
            "best_day": None,
            "worst_day": None,
            "first_trade_date": None,
            "active_days": 0,
            "avg_daily_pnl": 0,
            "inception_date": None,
        }

    # Günlük gruplama
    daily = {}
    for t in trades:
        ex = t.get("exit_time", "")
        if not ex: continue
        d = ex[:10]  # "YYYY-MM-DD"
        if d not in daily:
            daily[d] = 0
        daily[d] += t.get("pnl_try", 0) or 0

    total_pnl = sum(t.get("pnl_try", 0) or 0 for t in trades)
    winners = [t for t in trades if (t.get("pnl_try") or 0) > 0]

    best_day_date = max(daily.keys(), key=lambda k: daily[k]) if daily else None
    worst_day_date = min(daily.keys(), key=lambda k: daily[k]) if daily else None

    return {
        "starting_capital": starting_capital,
        "total_trades": len(trades),
        "n_winners": len(winners),
        "n_losers": len(trades) - len(winners),
        "total_pnl_try": round(total_pnl, 2),
        "total_pnl_pct": round((total_pnl / starting_capital * 100) if starting_capital > 0 else 0, 2),
        "win_rate_pct": round((len(winners) / len(trades) * 100) if trades else 0, 1),
        "best_day": {
            "date": best_day_date,
            "pnl": round(daily[best_day_date], 2) if best_day_date else 0,
        } if best_day_date else None,
        "worst_day": {
            "date": worst_day_date,
            "pnl": round(daily[worst_day_date], 2) if worst_day_date else 0,
        } if worst_day_date else None,
        "active_days": len(daily),
        "avg_daily_pnl": round(total_pnl / len(daily), 2) if daily else 0,
        "inception_date": trades[0].get("entry_time", "")[:10] if trades else None,
    }


def update_plan_status(date: str, user_status: str,
                      daily_budget_try: float = None,
                      daily_profit_target_try: float = None,
                      max_trades: int = None,
                      notes: str = "") -> dict:
    """Kullanıcı AI planını onaylar / düzenler / atlar."""
    init_daily_plan_table()
    fields = ["user_status=?"]
    params = [user_status]
    if daily_budget_try is not None:
        fields.append("daily_budget_try=?"); params.append(daily_budget_try)
    if daily_profit_target_try is not None:
        fields.append("daily_profit_target_try=?"); params.append(daily_profit_target_try)
    if max_trades is not None:
        fields.append("max_trades=?"); params.append(max_trades)
    if notes:
        fields.append("user_notes=?"); params.append(notes)
    params.append(date)

    with sqlite3.connect(_DB_PATH) as conn:
        conn.execute(f"UPDATE daily_plans SET {', '.join(fields)} WHERE date=?", params)
        conn.commit()
    return get_daily_plan(date)
