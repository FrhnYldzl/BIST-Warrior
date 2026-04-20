"""
database.py — SQLite İşlem Logu

Her işlemi trades.db dosyasına kaydeder.
Tablo: trades
"""

import sqlite3
import os
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(__file__), "trades.db")


def init_db():
    """Uygulama başlarken tabloyu oluştur (yoksa)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT    NOT NULL,
                ticker      TEXT    NOT NULL,
                action      TEXT    NOT NULL,
                price       REAL,
                qty         REAL,
                order_id    TEXT,
                status      TEXT,
                raw_signal  TEXT,
                raw_result  TEXT
            )
            """
        )
        conn.commit()


def log_trade(signal, result: dict):
    """
    Gelen sinyal ve broker sonucunu veritabanına kaydet.

    Args:
        signal : Pydantic Signal nesnesi (main.py'den gelir)
        result : Broker'dan dönen dict
    """
    import json

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO trades
                (timestamp, ticker, action, price, qty, order_id, status, raw_signal, raw_result)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.utcnow().isoformat(),
                signal.ticker,
                signal.action,
                signal.price,
                signal.qty,
                result.get("order_id"),
                result.get("status"),
                json.dumps(signal.model_dump(exclude={"secret"})),
                json.dumps(result),
            ),
        )
        conn.commit()


def get_recent_trades(limit: int = 20) -> list[dict]:
    """Son N islemi listele."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(row) for row in rows]


def clear_old_trades():
    """Tum islem gecmisini temizle (test verileri icin)."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM trades")
        conn.commit()
    return {"status": "ok", "message": "Tum islem gecmisi temizlendi"}
