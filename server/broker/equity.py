"""
equity.py — BIST Paper Trading Broker Modulu (Meridian TR V1)

Alpaca yerine yfinance + lokal SQLite simulasyonu.
Ayni EquityBroker API'sini saglar, boylece scheduler/risk_manager/main.py
degismeden calisir.

BIST Ozellikleri:
  - Ticker format: AKBNK.IS, GARAN.IS, THYAO.IS (yfinance formati)
  - Piyasa saati: 10:00-18:00 Istanbul (07:00-15:00 UTC), Pzt-Cum
  - Para birimi: TRY
  - Lot size: 1 hisse (BIST'te minimum lot kaldirildi 2020'de)
  - PDT yok (TR'de pattern day trader kuralı yok — serbest)
  - Komisyon: %0.1 (simulasyon)

Guvenlik:
  - Order Loop Korumasi: 60sn cooldown
  - Piyasa Saati Kontrolu
  - Fiyat Dogrulama
  - Flash crash entegrasyonu
"""

import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import yfinance as yf
from dotenv import load_dotenv

from config import ORDER_COOLDOWN_SEC, BRACKET_ENABLED, DATA_DIR

load_dotenv()

# Simulasyon veritabani yolu (Railway Volume varsa /data, yoksa server/)
_DB_PATH = Path(DATA_DIR) / "paper_bist.db"

# Baslangic sermayesi (TRY)
STARTING_CAPITAL_TRY = float(os.getenv("STARTING_CAPITAL_TRY", "1000000"))

# Komisyon orani (alim+satim toplam)
COMMISSION_RATE = float(os.getenv("COMMISSION_RATE", "0.001"))  # %0.1


class EquityBroker:
    """BIST Paper Trading Broker — Alpaca API uyumlu."""

    def __init__(self):
        self._recent_orders: dict[str, float] = {}
        self._order_cooldown = ORDER_COOLDOWN_SEC
        self._init_db()

    # ─── Database Init ────────────────────────────────────────────

    def _init_db(self):
        with sqlite3.connect(_DB_PATH) as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS account (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    cash REAL NOT NULL,
                    starting_capital REAL NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    ticker TEXT PRIMARY KEY,
                    qty REAL NOT NULL,
                    avg_entry_price REAL NOT NULL,
                    opened_at TEXT NOT NULL,
                    side TEXT DEFAULT 'long'
                );
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    ticker TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    commission REAL,
                    status TEXT,
                    submitted_at TEXT,
                    filled_at TEXT,
                    stop_loss REAL,
                    take_profit REAL,
                    order_class TEXT
                );
            """)
            # Hesap yoksa olustur
            row = conn.execute("SELECT id FROM account WHERE id=1").fetchone()
            if not row:
                conn.execute(
                    "INSERT INTO account (id, cash, starting_capital, created_at) VALUES (1, ?, ?, ?)",
                    (STARTING_CAPITAL_TRY, STARTING_CAPITAL_TRY, datetime.now(timezone.utc).isoformat())
                )
                print(f"[BIST Broker] Paper hesap olusturuldu: ₺{STARTING_CAPITAL_TRY:,.2f}")
            conn.commit()

    # ─── Ana islem metodu ─────────────────────────────────────────

    def execute(
        self, action: str, ticker: str, qty: float, price: float,
        stop_loss: float = None, take_profit: float = None,
        order_type: str = "market",
    ) -> dict:
        action = action.lower().strip()
        ticker = self._normalize_ticker(ticker)

        # 1. Piyasa saati kontrolu
        if action in ("long", "short"):
            market_check = self._check_market_hours()
            if not market_check["open"]:
                return {"status": "rejected", "ticker": ticker, "reason": f"BIST kapali. {market_check['message']}", "action_blocked": action}

        # 2. Order Loop korumasi
        loop_check = self._check_order_loop(ticker, action)
        if not loop_check["allowed"]:
            return {"status": "rejected", "ticker": ticker, "reason": loop_check["reason"], "action_blocked": action, "cooldown_remaining": loop_check.get("remaining", 0)}

        # 3. Fiyat dogrulama
        if price > 0 and action in ("long", "short"):
            price_check = self._validate_price(ticker, price)
            if not price_check["valid"]:
                return {"status": "rejected", "ticker": ticker, "reason": price_check["reason"], "action_blocked": action}

        # 4. Emri gonder
        if action == "long":
            result = self._buy(ticker, qty, price, stop_loss, take_profit, order_type)
        elif action == "short":
            # BIST'te açığa satış sınırlı — şimdilik engelle
            return {"status": "rejected", "ticker": ticker, "reason": "BIST'te short satış sınırlı (paper broker desteklemiyor)", "action_blocked": action}
        elif action in ("close_long", "close_short"):
            result = self._close_position(ticker)
        else:
            raise ValueError(f"Bilinmeyen aksiyon: '{action}'")

        # 5. Basarili emri kaydet
        if result.get("status") not in ("error", "rejected"):
            self._recent_orders[ticker] = time.time()

        return result

    # ─── Alim (Long) ──────────────────────────────────────────────

    def _buy(self, ticker: str, qty: float, price: float = 0,
             stop_loss: float = None, take_profit: float = None,
             order_type: str = "market") -> dict:
        qty = max(1, int(qty))

        # Fiyat al
        if price <= 0 or order_type == "market":
            current_price = self._get_current_price(ticker)
            if current_price is None or current_price <= 0:
                return {"status": "error", "ticker": ticker, "message": "Guncel fiyat alinamadi"}
            fill_price = current_price
        else:
            fill_price = price

        total_cost = qty * fill_price
        commission = total_cost * COMMISSION_RATE

        # Nakit yeterli mi?
        with sqlite3.connect(_DB_PATH) as conn:
            cash = conn.execute("SELECT cash FROM account WHERE id=1").fetchone()[0]
            if cash < total_cost + commission:
                return {"status": "rejected", "ticker": ticker, "reason": f"Yetersiz bakiye: ₺{cash:,.2f} < ₺{total_cost + commission:,.2f}"}

            # Mevcut pozisyon var mi?
            existing = conn.execute("SELECT qty, avg_entry_price FROM positions WHERE ticker=?", (ticker,)).fetchone()
            if existing:
                old_qty, old_avg = existing
                new_qty = old_qty + qty
                new_avg = (old_qty * old_avg + qty * fill_price) / new_qty
                conn.execute("UPDATE positions SET qty=?, avg_entry_price=? WHERE ticker=?",
                             (new_qty, new_avg, ticker))
            else:
                conn.execute(
                    "INSERT INTO positions (ticker, qty, avg_entry_price, opened_at, side) VALUES (?,?,?,?,?)",
                    (ticker, qty, fill_price, datetime.now(timezone.utc).isoformat(), "long")
                )

            # Nakit azalt
            conn.execute("UPDATE account SET cash = cash - ? WHERE id=1", (total_cost + commission,))

            # Emir logla
            order_id = f"bist_{int(time.time() * 1000)}_{ticker}"
            conn.execute(
                """INSERT INTO orders
                   (order_id, ticker, side, qty, price, commission, status, submitted_at, filled_at,
                    stop_loss, take_profit, order_class)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (order_id, ticker, "buy", qty, fill_price, commission, "filled",
                 datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(),
                 stop_loss, take_profit, "bracket" if stop_loss and take_profit else "market")
            )
            conn.commit()

        result = {
            "order_id": order_id, "ticker": ticker, "side": "buy",
            "qty": str(qty), "status": "filled",
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "fill_price": fill_price, "commission": round(commission, 2),
            "total_cost": round(total_cost + commission, 2),
        }
        if stop_loss and take_profit:
            result["order_class"] = "bracket"
            result["stop_loss"] = round(stop_loss, 2)
            result["take_profit"] = round(take_profit, 2)
        return result

    # ─── Pozisyon Kapatma ─────────────────────────────────────────

    def _close_position(self, ticker: str) -> dict:
        with sqlite3.connect(_DB_PATH) as conn:
            pos = conn.execute("SELECT qty, avg_entry_price FROM positions WHERE ticker=?", (ticker,)).fetchone()
            if not pos:
                return {"status": "error", "ticker": ticker, "message": "Pozisyon bulunamadi"}

            qty, avg_entry = pos
            current_price = self._get_current_price(ticker)
            if current_price is None or current_price <= 0:
                return {"status": "error", "ticker": ticker, "message": "Guncel fiyat alinamadi"}

            proceeds = qty * current_price
            commission = proceeds * COMMISSION_RATE
            net = proceeds - commission
            pnl = net - (qty * avg_entry)

            # Pozisyon sil, nakit artir
            conn.execute("DELETE FROM positions WHERE ticker=?", (ticker,))
            conn.execute("UPDATE account SET cash = cash + ? WHERE id=1", (net,))

            order_id = f"bist_{int(time.time() * 1000)}_{ticker}"
            conn.execute(
                """INSERT INTO orders
                   (order_id, ticker, side, qty, price, commission, status, submitted_at, filled_at, order_class)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (order_id, ticker, "sell", qty, current_price, commission, "filled",
                 datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat(), "close")
            )
            conn.commit()

        return {
            "status": "closed", "ticker": ticker, "qty": qty,
            "fill_price": current_price, "proceeds": round(net, 2),
            "realized_pnl": round(pnl, 2), "commission": round(commission, 2),
        }

    # ─── Guvenlik Kontrolleri ─────────────────────────────────────

    def _check_order_loop(self, ticker: str, action: str) -> dict:
        if action in ("close_long", "close_short"):
            return {"allowed": True}
        now = time.time()
        last = self._recent_orders.get(ticker, 0)
        elapsed = now - last
        if elapsed < self._order_cooldown:
            remaining = round(self._order_cooldown - elapsed)
            return {"allowed": False, "reason": f"Order loop: {ticker} icin {remaining}sn beklenmeli.", "remaining": remaining}
        cutoff = now - 300
        self._recent_orders = {k: v for k, v in self._recent_orders.items() if v > cutoff}
        return {"allowed": True}

    def _check_market_hours(self) -> dict:
        """BIST: Pzt-Cum, 10:00-18:00 Istanbul (TRT = UTC+3)."""
        now_utc = datetime.now(timezone.utc)

        # Istanbul time = UTC + 3
        now_tr = now_utc + timedelta(hours=3)

        # Hafta sonu
        if now_tr.weekday() >= 5:
            return {"open": False, "message": f"Hafta sonu (TR saati: {now_tr.strftime('%H:%M')})"}

        # Saat kontrolu (TRT)
        if 10 <= now_tr.hour < 18:
            return {"open": True, "message": f"BIST acik (TR: {now_tr.strftime('%H:%M')})"}

        # Kapali
        if now_tr.hour < 10:
            open_time = now_tr.replace(hour=10, minute=0, second=0, microsecond=0)
        else:
            # Sonraki is gunu
            next_day = now_tr + timedelta(days=1)
            while next_day.weekday() >= 5:
                next_day += timedelta(days=1)
            open_time = next_day.replace(hour=10, minute=0, second=0, microsecond=0)

        return {"open": False, "message": f"Sonraki acilis: {open_time.strftime('%Y-%m-%d %H:%M')} TR"}

    def _validate_price(self, ticker: str, signal_price: float) -> dict:
        try:
            current = self._get_current_price(ticker)
            if current and current > 0:
                diff_pct = abs(current - signal_price) / current * 100
                if diff_pct > 20:
                    return {"valid": False, "reason": f"FIYAT UYUMSUZLUGU: Sinyal ₺{signal_price:.2f} vs gercek ₺{current:.2f} (fark: %{diff_pct:.0f})"}
            return {"valid": True}
        except Exception:
            return {"valid": True}

    # ─── Yardimcilar ──────────────────────────────────────────────

    @staticmethod
    def _normalize_ticker(ticker: str) -> str:
        """BIST ticker'ini yfinance formatina ('.IS' suffix) cevir."""
        ticker = ticker.upper().strip()
        if not ticker.endswith(".IS"):
            ticker = ticker + ".IS"
        return ticker

    @staticmethod
    def _get_current_price(ticker: str) -> Optional[float]:
        """yfinance ile guncel fiyat al."""
        try:
            ticker = ticker if ticker.endswith(".IS") else ticker + ".IS"
            t = yf.Ticker(ticker)
            # Fast info dene (rate-limit'e daha az yakalanir)
            try:
                fast = t.fast_info
                price = fast.get("lastPrice") or fast.get("regularMarketPrice")
                if price and price > 0:
                    return float(price)
            except Exception:
                pass
            # Fallback: son barin close fiyati
            hist = t.history(period="1d", interval="1m")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
            hist = t.history(period="5d")
            if not hist.empty:
                return float(hist["Close"].iloc[-1])
            return None
        except Exception as e:
            print(f"[BIST Broker] Fiyat alma hatasi ({ticker}): {e}")
            return None

    # ─── Flash Crash — Tum pozisyonlari kapat ────────────────────

    def emergency_liquidate(self) -> dict:
        try:
            closed = []
            with sqlite3.connect(_DB_PATH) as conn:
                rows = conn.execute("SELECT ticker FROM positions").fetchall()
                tickers = [r[0] for r in rows]
            for t in tickers:
                try:
                    self._close_position(t)
                    closed.append(t)
                except Exception as e:
                    closed.append(f"{t} (HATA: {e})")
            return {"status": "liquidated", "closed": closed, "count": len(closed)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def cancel_all_orders(self) -> dict:
        # Paper broker'da bekleyen emir tutmuyoruz (market orders anında fill)
        return {"status": "ok", "cancelled_count": 0, "message": "Paper broker: bekleyen emir yok"}

    def get_pending_orders(self) -> list:
        return []

    # ─── Hesap Bilgileri ──────────────────────────────────────────

    def get_balance(self) -> float:
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute("SELECT cash FROM account WHERE id=1").fetchone()
            return float(row[0]) if row else 0.0

    def get_account_status(self) -> dict:
        try:
            with sqlite3.connect(_DB_PATH) as conn:
                conn.row_factory = sqlite3.Row
                acc = conn.execute("SELECT * FROM account WHERE id=1").fetchone()
                positions = conn.execute("SELECT * FROM positions").fetchall()

            cash = float(acc["cash"])
            starting = float(acc["starting_capital"])

            # Pozisyon piyasa degerlerini hesapla
            market_value = 0
            for p in positions:
                current = self._get_current_price(p["ticker"]) or p["avg_entry_price"]
                market_value += p["qty"] * current

            equity = cash + market_value
            portfolio_value = equity

            return {
                "cash": cash,
                "equity": equity,
                "buying_power": cash,  # TR'de margin yok (basit varsayim)
                "portfolio_value": portfolio_value,
                "starting_capital": starting,
                "total_return_pct": round((equity - starting) / starting * 100, 2) if starting > 0 else 0,
                "market_value": market_value,
                "daytrade_count": 0,  # BIST'te PDT yok
                "pdt_check": "OK",
                "pattern_day_trader": False,
                "trading_blocked": False,
                "account_blocked": False,
                "currency": "TRY",
                "n_positions": len(positions),
            }
        except Exception as e:
            return {"error": str(e)}

    def get_position(self, ticker: str) -> dict | None:
        ticker = self._normalize_ticker(ticker)
        with sqlite3.connect(_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            pos = conn.execute("SELECT * FROM positions WHERE ticker=?", (ticker,)).fetchone()
            if not pos:
                return None
            current_price = self._get_current_price(ticker) or pos["avg_entry_price"]
            qty = float(pos["qty"])
            avg = float(pos["avg_entry_price"])
            return {
                "ticker": ticker, "qty": qty,
                "avg_entry_price": avg,
                "current_price": current_price,
                "unrealized_pl": (current_price - avg) * qty,
            }

    def get_all_positions(self) -> list:
        """Tum acik pozisyonlari dondur (main.py icin)."""
        with sqlite3.connect(_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM positions").fetchall()
        result = []
        for p in rows:
            current = self._get_current_price(p["ticker"]) or p["avg_entry_price"]
            qty = float(p["qty"])
            avg = float(p["avg_entry_price"])
            result.append({
                "ticker": p["ticker"],
                "symbol": p["ticker"],  # Alpaca uyumlulugu
                "qty": qty,
                "avg_entry_price": avg,
                "current_price": current,
                "market_value": qty * current,
                "unrealized_pl": (current - avg) * qty,
                "unrealized_plpc": round(((current - avg) / avg) * 100, 2) if avg > 0 else 0,
                "side": p["side"],
            })
        return result
