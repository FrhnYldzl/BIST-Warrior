"""
ibkr.py — Interactive Brokers (IBKR) BIST Broker (Meridian TR V1)

IB Gateway veya TWS uzerinden BIST'te paper trading.
Ayni EquityBroker API'sini saglar — scheduler/risk_manager degismeden calisir.

ONEMLI:
  Bu modul calismak icin IB Gateway veya TWS masaustu uygulamasinin
  localhost'ta calismasi gerekir. IBKR sunuculari direkt baglanamaz.

Baglanti Ayarlari:
  - Paper Trading: port 7497 (varsayilan IB Gateway paper)
  - Live Trading: port 7496
  - TWS Paper: port 7497
  - TWS Live: port 7496

BIST Ozellikleri:
  - Exchange: "BIST" (Istanbul Borsasi)
  - Currency: "TRY"
  - SecType: "STK"
  - Primary Exchange: "IBIS" (BIST)
  - Ticker: AKBNK, GARAN, THYAO (IBKR formati — .IS suffix yok)

IBKR BIST Piyasa Verisi:
  - Subscribe et: "Turkey Bundle" (aylik $2-3) — opsiyonel
  - Aksi halde sadece emir gonderebilir, veri yfinance'ten alinir (hibrit)
"""

import os
import time
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional

from dotenv import load_dotenv

from config import ORDER_COOLDOWN_SEC, BRACKET_ENABLED

load_dotenv()

# IBKR Baglanti
IBKR_HOST = os.getenv("IBKR_HOST", "127.0.0.1")
IBKR_PORT = int(os.getenv("IBKR_PORT", "7497"))  # Paper Gateway
IBKR_CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", "1"))
IBKR_USE_MARKET_DATA = os.getenv("IBKR_USE_MARKET_DATA", "false").lower() in ("true", "1", "yes")

# BIST veri fallback
try:
    import yfinance as yf
    _YFINANCE_AVAILABLE = True
except ImportError:
    _YFINANCE_AVAILABLE = False

try:
    from ib_insync import IB, Stock, MarketOrder, LimitOrder, StopOrder, Order, util
    _IB_AVAILABLE = True
except ImportError:
    _IB_AVAILABLE = False
    print("[IBKR] ib_insync yuklu degil — IBKR broker kullanilamaz")


class EquityBroker:
    """IBKR Paper/Live BIST Broker — Alpaca API uyumlu."""

    def __init__(self):
        if not _IB_AVAILABLE:
            raise ImportError("ib_insync gerekli: pip install ib_insync")

        self.ib = IB()
        self._recent_orders: dict[str, float] = {}
        self._order_cooldown = ORDER_COOLDOWN_SEC
        self._lock = threading.Lock()

        self._connect()

    # ─── Connection ───────────────────────────────────────────────

    def _connect(self):
        """IB Gateway'e baglan."""
        if self.ib.isConnected():
            return
        try:
            self.ib.connect(IBKR_HOST, IBKR_PORT, clientId=IBKR_CLIENT_ID, timeout=10)
            print(f"[IBKR] Baglandi: {IBKR_HOST}:{IBKR_PORT} (clientId={IBKR_CLIENT_ID})")
        except Exception as e:
            print(f"[IBKR] BAGLANTI HATASI: {e}")
            print(f"[IBKR] IB Gateway veya TWS calisiyor mu? Port {IBKR_PORT} acik mi?")
            raise

    def _ensure_connected(self):
        if not self.ib.isConnected():
            self._connect()

    def disconnect(self):
        if self.ib.isConnected():
            self.ib.disconnect()

    # ─── Ana Islem Metodu ─────────────────────────────────────────

    def execute(
        self, action: str, ticker: str, qty: float, price: float,
        stop_loss: float = None, take_profit: float = None,
        order_type: str = "market",
    ) -> dict:
        action = action.lower().strip()
        ticker = self._clean_ticker(ticker)

        # 1. Piyasa saati kontrolu
        if action in ("long", "short"):
            market_check = self._check_market_hours()
            if not market_check["open"]:
                return {"status": "rejected", "ticker": ticker, "reason": f"BIST kapali. {market_check['message']}", "action_blocked": action}

        # 2. Order Loop korumasi
        loop_check = self._check_order_loop(ticker, action)
        if not loop_check["allowed"]:
            return {"status": "rejected", "ticker": ticker, "reason": loop_check["reason"], "action_blocked": action}

        # 3. Fiyat dogrulama
        if price > 0 and action in ("long", "short"):
            price_check = self._validate_price(ticker, price)
            if not price_check["valid"]:
                return {"status": "rejected", "ticker": ticker, "reason": price_check["reason"], "action_blocked": action}

        # 4. Emri gonder
        try:
            self._ensure_connected()
            if action == "long":
                result = self._buy(ticker, qty, price, stop_loss, take_profit, order_type)
            elif action == "short":
                return {"status": "rejected", "ticker": ticker, "reason": "BIST'te short satış sınırlı", "action_blocked": action}
            elif action in ("close_long", "close_short"):
                result = self._close_position(ticker)
            else:
                raise ValueError(f"Bilinmeyen aksiyon: '{action}'")
        except Exception as e:
            return {"status": "error", "ticker": ticker, "message": f"IBKR hata: {e}"}

        if result.get("status") not in ("error", "rejected"):
            self._recent_orders[ticker] = time.time()

        return result

    # ─── BIST Contract ────────────────────────────────────────────

    def _bist_contract(self, ticker: str) -> "Stock":
        """BIST hissesi icin IBKR Contract olustur."""
        return Stock(
            symbol=ticker,
            exchange="IBIS",        # BIST primary exchange
            currency="TRY",
        )

    # ─── Alim (Long) ──────────────────────────────────────────────

    def _buy(self, ticker: str, qty: float, price: float = 0,
             stop_loss: float = None, take_profit: float = None,
             order_type: str = "market") -> dict:
        qty = max(1, int(qty))
        contract = self._bist_contract(ticker)

        # Contract qualify (IBKR'de zorunlu)
        self.ib.qualifyContracts(contract)

        # Ana emir
        if order_type == "limit" and price > 0:
            parent = LimitOrder("BUY", qty, round(price, 2))
        else:
            parent = MarketOrder("BUY", qty)

        parent.tif = "DAY"

        # Bracket order?
        if BRACKET_ENABLED and stop_loss and take_profit:
            parent.transmit = False
            parent.orderId = self.ib.client.getReqId()

            tp_order = LimitOrder("SELL", qty, round(take_profit, 2))
            tp_order.parentId = parent.orderId
            tp_order.transmit = False
            tp_order.tif = "GTC"

            sl_order = StopOrder("SELL", qty, round(stop_loss, 2))
            sl_order.parentId = parent.orderId
            sl_order.transmit = True  # Son emir tetikler
            sl_order.tif = "GTC"

            trade_parent = self.ib.placeOrder(contract, parent)
            trade_tp = self.ib.placeOrder(contract, tp_order)
            trade_sl = self.ib.placeOrder(contract, sl_order)

            self.ib.sleep(1)  # Emir acknowledge icin

            return {
                "order_id": str(trade_parent.order.orderId),
                "ticker": ticker,
                "side": "buy",
                "qty": str(qty),
                "status": str(trade_parent.orderStatus.status).lower(),
                "submitted_at": datetime.now(timezone.utc).isoformat(),
                "order_class": "bracket",
                "stop_loss": round(stop_loss, 2),
                "take_profit": round(take_profit, 2),
                "fill_price": self._get_fill_price(trade_parent),
            }

        # Basit market/limit order
        trade = self.ib.placeOrder(contract, parent)
        self.ib.sleep(1)

        return {
            "order_id": str(trade.order.orderId),
            "ticker": ticker,
            "side": "buy",
            "qty": str(qty),
            "status": str(trade.orderStatus.status).lower(),
            "submitted_at": datetime.now(timezone.utc).isoformat(),
            "order_type": order_type,
            "fill_price": self._get_fill_price(trade),
        }

    # ─── Pozisyon Kapatma ─────────────────────────────────────────

    def _close_position(self, ticker: str) -> dict:
        positions = self.ib.positions()
        pos = next((p for p in positions if p.contract.symbol == ticker), None)

        if not pos:
            return {"status": "error", "ticker": ticker, "message": "Pozisyon bulunamadi"}

        qty = abs(int(pos.position))
        if qty == 0:
            return {"status": "error", "ticker": ticker, "message": "Pozisyon miktari 0"}

        contract = self._bist_contract(ticker)
        self.ib.qualifyContracts(contract)

        # Long pozisyon → SELL ile kapat
        # Short pozisyon → BUY ile kapat
        side = "SELL" if pos.position > 0 else "BUY"
        order = MarketOrder(side, qty)
        order.tif = "DAY"

        trade = self.ib.placeOrder(contract, order)
        self.ib.sleep(1)

        return {
            "status": "closed",
            "ticker": ticker,
            "qty": qty,
            "side": side.lower(),
            "fill_price": self._get_fill_price(trade),
            "order_id": str(trade.order.orderId),
        }

    @staticmethod
    def _get_fill_price(trade) -> Optional[float]:
        """Trade'in fill fiyatini al."""
        try:
            if trade.fills:
                return float(trade.fills[0].execution.price)
            return float(trade.orderStatus.avgFillPrice) if trade.orderStatus.avgFillPrice else None
        except Exception:
            return None

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
        now_tr = now_utc + timedelta(hours=3)

        if now_tr.weekday() >= 5:
            return {"open": False, "message": f"Hafta sonu (TR: {now_tr.strftime('%H:%M')})"}
        if 10 <= now_tr.hour < 18:
            return {"open": True, "message": f"BIST acik (TR: {now_tr.strftime('%H:%M')})"}

        if now_tr.hour < 10:
            open_time = now_tr.replace(hour=10, minute=0, second=0, microsecond=0)
        else:
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

    def _get_current_price(self, ticker: str) -> Optional[float]:
        """Guncel fiyat al — IBKR (subscribed ise) veya yfinance fallback."""
        # IBKR data subscription varsa
        if IBKR_USE_MARKET_DATA:
            try:
                contract = self._bist_contract(ticker)
                self.ib.qualifyContracts(contract)
                t = self.ib.reqMktData(contract, "", False, False)
                self.ib.sleep(2)
                price = t.last or t.close or t.marketPrice()
                self.ib.cancelMktData(contract)
                if price and price > 0:
                    return float(price)
            except Exception as e:
                print(f"[IBKR] Market data hata ({ticker}): {e}")

        # Fallback: yfinance
        if _YFINANCE_AVAILABLE:
            try:
                yticker = ticker if ticker.endswith(".IS") else ticker + ".IS"
                t = yf.Ticker(yticker)
                try:
                    fast = t.fast_info
                    price = fast.get("lastPrice") or fast.get("regularMarketPrice")
                    if price and price > 0:
                        return float(price)
                except Exception:
                    pass
                hist = t.history(period="1d", interval="1m")
                if not hist.empty:
                    return float(hist["Close"].iloc[-1])
            except Exception:
                pass
        return None

    # ─── Flash Crash ──────────────────────────────────────────────

    def emergency_liquidate(self) -> dict:
        try:
            self._ensure_connected()
            positions = self.ib.positions()
            closed = []
            for pos in positions:
                try:
                    self._close_position(pos.contract.symbol)
                    closed.append(pos.contract.symbol)
                except Exception as e:
                    closed.append(f"{pos.contract.symbol} (HATA: {e})")
            self.cancel_all_orders()
            return {"status": "liquidated", "closed": closed, "count": len(closed)}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def cancel_all_orders(self) -> dict:
        try:
            self._ensure_connected()
            self.ib.reqGlobalCancel()
            return {"status": "ok", "message": "Tum emirler iptal edildi"}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def get_pending_orders(self) -> list:
        try:
            self._ensure_connected()
            trades = self.ib.openTrades()
            return [
                {
                    "order_id": str(t.order.orderId),
                    "ticker": t.contract.symbol,
                    "side": t.order.action.lower(),
                    "qty": str(t.order.totalQuantity),
                    "status": str(t.orderStatus.status).lower(),
                    "type": t.order.orderType,
                    "submitted_at": "",
                }
                for t in trades
            ]
        except Exception as e:
            return [{"error": str(e)}]

    # ─── Hesap Bilgileri ──────────────────────────────────────────

    def get_balance(self) -> float:
        try:
            self._ensure_connected()
            acc = self.ib.accountSummary()
            for v in acc:
                if v.tag == "TotalCashValue" and v.currency == "TRY":
                    return float(v.value)
                if v.tag == "TotalCashValue" and v.currency == "BASE":
                    return float(v.value)
            return 0.0
        except Exception:
            return 0.0

    def get_account_status(self) -> dict:
        try:
            self._ensure_connected()
            acc = self.ib.accountSummary()

            summary = {}
            for v in acc:
                key = v.tag
                if key in ("TotalCashValue", "NetLiquidation", "BuyingPower",
                          "GrossPositionValue", "UnrealizedPnL", "RealizedPnL"):
                    if v.currency == "TRY" or v.currency == "BASE":
                        summary[key] = float(v.value)

            positions = self.ib.positions()
            n_positions = len(positions)

            return {
                "cash": summary.get("TotalCashValue", 0),
                "equity": summary.get("NetLiquidation", 0),
                "buying_power": summary.get("BuyingPower", 0),
                "portfolio_value": summary.get("NetLiquidation", 0),
                "market_value": summary.get("GrossPositionValue", 0),
                "unrealized_pnl": summary.get("UnrealizedPnL", 0),
                "realized_pnl": summary.get("RealizedPnL", 0),
                "daytrade_count": 0,  # TR'de PDT yok
                "pdt_check": "OK",
                "pattern_day_trader": False,
                "trading_blocked": False,
                "account_blocked": False,
                "currency": "TRY",
                "n_positions": n_positions,
                "broker": "IBKR",
            }
        except Exception as e:
            return {"error": f"IBKR hesap hata: {e}"}

    def get_position(self, ticker: str) -> dict | None:
        try:
            self._ensure_connected()
            ticker = self._clean_ticker(ticker)
            positions = self.ib.positions()
            pos = next((p for p in positions if p.contract.symbol == ticker), None)
            if not pos:
                return None

            qty = float(pos.position)
            avg = float(pos.avgCost)
            current = self._get_current_price(ticker) or avg

            return {
                "ticker": ticker, "qty": qty,
                "avg_entry_price": avg,
                "current_price": current,
                "unrealized_pl": (current - avg) * qty,
            }
        except Exception:
            return None

    def get_all_positions(self) -> list:
        try:
            self._ensure_connected()
            positions = self.ib.positions()
            result = []
            for p in positions:
                ticker = p.contract.symbol
                qty = float(p.position)
                avg = float(p.avgCost)
                current = self._get_current_price(ticker) or avg
                result.append({
                    "ticker": ticker,
                    "symbol": ticker,
                    "qty": qty,
                    "avg_entry_price": avg,
                    "current_price": current,
                    "market_value": qty * current,
                    "unrealized_pl": (current - avg) * qty,
                    "unrealized_plpc": round(((current - avg) / avg) * 100, 2) if avg > 0 else 0,
                    "side": "long" if qty > 0 else "short",
                })
            return result
        except Exception as e:
            print(f"[IBKR] Position listesi hata: {e}")
            return []

    # ─── Yardimcilar ──────────────────────────────────────────────

    @staticmethod
    def _clean_ticker(ticker: str) -> str:
        """IBKR formatina cevir (.IS suffix kaldir)."""
        ticker = ticker.upper().strip()
        if ticker.endswith(".IS"):
            ticker = ticker[:-3]
        return ticker
