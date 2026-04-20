"""
risk_manager.py — Dinamik Risk Yönetimi Motoru (V3)

V1: Sabit %2 risk
V2: Claude güven skoruna göre dinamik pozisyon boyutlandirma
V3: Config-driven + sektör diversifikasyon + flash crash failsafe + Sharpe/VaR

Özellikler:
  - Güven skoru bazli pozisyon büyüklügü (1-10 skala)
  - Rejime göre risk limiti ayarlama
  - ATR-bazli stop-loss hesaplama
  - Max drawdown korumasi
  - Portföy yogunlasma + sektör limiti
  - Flash crash failsafe
  - Sharpe Ratio / Sortino / VaR hesaplama
"""

import math
from config import (
    MAX_RISK_PCT, MAX_POSITION_PCT, MAX_SECTOR_PCT,
    ATR_MULTIPLIER, FLASH_CRASH_THRESHOLD,
    CONFIDENCE_RISK_MAP, REGIME_MULTIPLIERS, REGIME_MAX_INVESTED,
    SECTOR_MAP,
)


class RiskManager:
    def __init__(self, max_risk_pct: float = None):
        self.max_risk_pct = max_risk_pct or MAX_RISK_PCT

    # ─────────────────────────────────────────────────────────────
    # V3: Dinamik Pozisyon Boyutlandirma (config-driven)
    # ─────────────────────────────────────────────────────────────

    def dynamic_position_size(
        self,
        equity: float,
        entry_price: float,
        stop_loss_price: float,
        confidence: int = 5,
        regime: str = "neutral",
    ) -> dict:
        if entry_price <= 0 or stop_loss_price <= 0 or equity <= 0:
            return {"qty": 0, "risk_amount": 0, "risk_pct": 0, "error": "Gecersiz parametreler"}

        # 1. Güven skoruna göre risk yüzdesi
        risk_pct = self._confidence_to_risk(confidence)

        # 2. Rejime göre risk ayarlama
        risk_pct = self._regime_adjustment(risk_pct, regime)

        # 3. Mutlak max risk limiti
        risk_pct = min(risk_pct, self.max_risk_pct)

        # 4. Risk miktari
        risk_amount = equity * risk_pct

        # 5. Stop mesafesi
        stop_distance = abs(entry_price - stop_loss_price)
        if stop_distance == 0:
            stop_distance = entry_price * 0.02

        stop_distance_pct = round(stop_distance / entry_price * 100, 2)

        # 6. Adet hesaplama
        qty = risk_amount / stop_distance
        qty = max(1, round(qty))

        # 7. Pozisyon degeri kontrolü (config'den max %)
        position_value = qty * entry_price
        max_position_value = equity * MAX_POSITION_PCT
        if position_value > max_position_value:
            qty = max(1, int(max_position_value / entry_price))
            position_value = qty * entry_price

        position_pct = round(position_value / equity * 100, 2) if equity > 0 else 0

        return {
            "qty": qty,
            "risk_amount": round(risk_amount, 2),
            "risk_pct": round(risk_pct * 100, 2),
            "position_value": round(position_value, 2),
            "position_pct": position_pct,
            "stop_distance": round(stop_distance, 2),
            "stop_distance_pct": stop_distance_pct,
        }

    def _confidence_to_risk(self, confidence: int) -> float:
        key = str(max(1, min(10, confidence)))
        return CONFIDENCE_RISK_MAP.get(key, 0.010)

    def _regime_adjustment(self, risk_pct: float, regime: str) -> float:
        return risk_pct * REGIME_MULTIPLIERS.get(regime, 0.7)

    # ─────────────────────────────────────────────────────────────
    # V1 Uyumlu Metodlar
    # ─────────────────────────────────────────────────────────────

    def calculate_position_size(
        self, balance: float, entry_price: float, stop_loss_pct: float = 0.02
    ) -> float:
        if entry_price <= 0 or stop_loss_pct <= 0:
            raise ValueError("Giris fiyati ve stop-loss yüzdesi sifirdan büyük olmali.")
        risk_amount = balance * self.max_risk_pct
        stop_loss_distance = entry_price * stop_loss_pct
        qty = risk_amount / stop_loss_distance
        return round(qty, 4)

    def calculate_stop_loss(self, entry_price: float, direction: str, pct: float = 0.02) -> float:
        if direction == "long":
            return round(entry_price * (1 - pct), 4)
        return round(entry_price * (1 + pct), 4)

    def calculate_take_profit(
        self, entry_price: float, direction: str, risk_reward: float = 2.0, stop_pct: float = 0.02
    ) -> float:
        tp_pct = stop_pct * risk_reward
        if direction == "long":
            return round(entry_price * (1 + tp_pct), 4)
        return round(entry_price * (1 - tp_pct), 4)

    # ─────────────────────────────────────────────────────────────
    # ATR Bazli Stop-Loss (V2) — config-driven multiplier
    # ─────────────────────────────────────────────────────────────

    def atr_stop_loss(
        self, entry_price: float, atr: float, direction: str, multiplier: float = None
    ) -> float:
        mult = multiplier or ATR_MULTIPLIER
        stop_distance = atr * mult
        if direction == "long":
            return round(entry_price - stop_distance, 2)
        return round(entry_price + stop_distance, 2)

    def atr_take_profit(
        self, entry_price: float, atr: float, direction: str,
        rr_ratio: float = 2.0, multiplier: float = None
    ) -> float:
        mult = multiplier or ATR_MULTIPLIER
        tp_distance = atr * mult * rr_ratio
        if direction == "long":
            return round(entry_price + tp_distance, 2)
        return round(entry_price - tp_distance, 2)

    # ─────────────────────────────────────────────────────────────
    # Trailing Stop (V2.2)
    # ─────────────────────────────────────────────────────────────

    def trailing_stop(
        self, entry_price: float, current_price: float, initial_stop: float,
        atr: float, direction: str = "long", method: str = "atr",
    ) -> dict:
        mult = ATR_MULTIPLIER
        if direction == "long":
            in_profit = current_price > entry_price
            if method == "atr":
                candidate_stop = round(current_price - atr * mult, 2)
            elif method == "percent":
                candidate_stop = round(current_price * 0.98, 2)
            elif method == "breakeven":
                candidate_stop = entry_price if in_profit else initial_stop
            else:
                candidate_stop = initial_stop

            new_stop = max(initial_stop, candidate_stop)
            stop_moved = new_stop > initial_stop
            profit_locked = round((new_stop - entry_price) / entry_price * 100, 2) if new_stop > entry_price else 0

            return {
                "new_stop": new_stop, "stop_moved": stop_moved, "initial_stop": initial_stop,
                "reason": f"{'ATR' if method == 'atr' else method} trailing: stop {'yukari tasindi' if stop_moved else 'ayni kaldi'}",
                "profit_locked_pct": profit_locked,
                "distance_to_stop": round(current_price - new_stop, 2),
                "distance_to_stop_pct": round((current_price - new_stop) / current_price * 100, 2),
                "in_profit": in_profit,
            }
        else:
            in_profit = current_price < entry_price
            if method == "atr":
                candidate_stop = round(current_price + atr * mult, 2)
            elif method == "percent":
                candidate_stop = round(current_price * 1.02, 2)
            elif method == "breakeven":
                candidate_stop = entry_price if in_profit else initial_stop
            else:
                candidate_stop = initial_stop

            new_stop = min(initial_stop, candidate_stop)
            stop_moved = new_stop < initial_stop
            profit_locked = round((entry_price - new_stop) / entry_price * 100, 2) if new_stop < entry_price else 0

            return {
                "new_stop": new_stop, "stop_moved": stop_moved, "initial_stop": initial_stop,
                "reason": f"{'ATR' if method == 'atr' else method} trailing: stop {'asagi tasindi' if stop_moved else 'ayni kaldi'}",
                "profit_locked_pct": profit_locked,
                "distance_to_stop": round(new_stop - current_price, 2),
                "distance_to_stop_pct": round((new_stop - current_price) / current_price * 100, 2),
                "in_profit": in_profit,
            }

    def check_exit_signals(
        self, entry_price: float, current_price: float,
        stop_loss: float, take_profit: float, direction: str = "long",
    ) -> dict:
        if direction == "long":
            pnl_pct = round((current_price - entry_price) / entry_price * 100, 2)
            if current_price <= stop_loss:
                return {"should_exit": True, "exit_reason": "stop_loss_hit", "pnl_pct": pnl_pct}
            if current_price >= take_profit:
                return {"should_exit": True, "exit_reason": "take_profit_hit", "pnl_pct": pnl_pct}
        else:
            pnl_pct = round((entry_price - current_price) / entry_price * 100, 2)
            if current_price >= stop_loss:
                return {"should_exit": True, "exit_reason": "stop_loss_hit", "pnl_pct": pnl_pct}
            if current_price <= take_profit:
                return {"should_exit": True, "exit_reason": "take_profit_hit", "pnl_pct": pnl_pct}
        return {"should_exit": False, "exit_reason": None, "pnl_pct": pnl_pct}

    # ─────────────────────────────────────────────────────────────
    # V3: Flash Crash Failsafe
    # ─────────────────────────────────────────────────────────────

    def check_flash_crash(self, positions: list, market_data: dict) -> dict:
        """
        Anlık büyük düşüş tespiti — failsafe tetikleme.
        Herhangi bir pozisyondaki hisse %5+ düşmüşse alarm verir.
        """
        alerts = []
        should_liquidate = False

        for pos in positions:
            ticker = pos.get("ticker", "")
            data = market_data.get(ticker, {})
            change_pct = data.get("change_pct", 0)

            if change_pct <= -(FLASH_CRASH_THRESHOLD * 100):
                alerts.append({
                    "ticker": ticker,
                    "change_pct": change_pct,
                    "severity": "CRITICAL",
                    "action": "LIQUIDATE",
                })
                should_liquidate = True

        # Genel piyasa kontrolü (SPY)
        spy_data = market_data.get("SPY", {})
        spy_change = spy_data.get("change_pct", 0)
        if spy_change <= -(FLASH_CRASH_THRESHOLD * 100):
            alerts.append({
                "ticker": "SPY (MARKET)",
                "change_pct": spy_change,
                "severity": "SYSTEMIC",
                "action": "HALT_ALL_TRADING",
            })
            should_liquidate = True

        return {
            "flash_crash_detected": should_liquidate,
            "alerts": alerts,
            "threshold": FLASH_CRASH_THRESHOLD * 100,
        }

    # ─────────────────────────────────────────────────────────────
    # V3: Sektör Diversifikasyon Kontrolü
    # ─────────────────────────────────────────────────────────────

    def check_sector_exposure(self, equity: float, positions: list) -> dict:
        """
        Sektör bazlı yoğunlaşma kontrolü.
        Max %40 tek sektör kuralı (config'den okunur).
        """
        if equity <= 0:
            return {"sectors": {}, "warnings": [], "compliant": True}

        sector_values = {}
        for pos in positions:
            ticker = pos.get("ticker", "")
            value = pos.get("qty", 0) * pos.get("current_price", 0)
            sector = SECTOR_MAP.get(ticker, "Unknown")
            sector_values[sector] = sector_values.get(sector, 0) + value

        sector_pcts = {}
        warnings = []
        compliant = True

        for sector, value in sector_values.items():
            pct = round(value / equity * 100, 1)
            sector_pcts[sector] = {"value": round(value, 2), "pct": pct}

            if sector not in ("ETF",) and pct > MAX_SECTOR_PCT * 100:
                warnings.append(
                    f"{sector} sektörü portföyün %{pct}'i — max %{MAX_SECTOR_PCT*100:.0f} limiti aşıldı"
                )
                compliant = False

        return {
            "sectors": sector_pcts,
            "warnings": warnings,
            "compliant": compliant,
            "max_sector_pct": MAX_SECTOR_PCT * 100,
        }

    # ─────────────────────────────────────────────────────────────
    # Portföy Risk Kontrolü (V3 — sektör dahil)
    # ─────────────────────────────────────────────────────────────

    def portfolio_risk_check(self, equity: float, positions: list, regime: str = "neutral") -> dict:
        if equity <= 0:
            return {"total_invested_pct": 0, "cash_pct": 100, "warnings": ["Portföy degeri 0"]}

        total_invested = sum(p.get("qty", 0) * p.get("current_price", 0) for p in positions)
        invested_pct = round(total_invested / equity * 100, 1)
        cash_pct = round(100 - invested_pct, 1)

        largest_pct = 0
        if positions:
            largest = max(positions, key=lambda p: p.get("qty", 0) * p.get("current_price", 0))
            largest_val = largest.get("qty", 0) * largest.get("current_price", 0)
            largest_pct = round(largest_val / equity * 100, 1)

        warnings = []

        # Rejim uyumu
        max_invested = REGIME_MAX_INVESTED.get(regime, 70)
        regime_compliant = invested_pct <= max_invested
        if not regime_compliant:
            warnings.append(f"Rejim ({regime}) icin max %{max_invested} yatirim önerilir, su an %{invested_pct}")

        if largest_pct > MAX_POSITION_PCT * 100:
            warnings.append(f"En büyük pozisyon portföyün %{largest_pct}'i — yogunlasma riski")

        if cash_pct < 10:
            warnings.append(f"Nakit orani cok düsük (%{cash_pct}) — acil durum tampon yok")

        # V3: Sektör kontrolü
        sector_check = self.check_sector_exposure(equity, positions)
        warnings.extend(sector_check["warnings"])

        return {
            "total_invested_pct": invested_pct,
            "cash_pct": cash_pct,
            "largest_position_pct": largest_pct,
            "regime_compliant": regime_compliant,
            "max_invested_for_regime": max_invested,
            "sector_exposure": sector_check["sectors"],
            "sector_compliant": sector_check["compliant"],
            "warnings": warnings,
        }

    # ─────────────────────────────────────────────────────────────
    # V3: Sharpe Ratio / Sortino / VaR
    # ─────────────────────────────────────────────────────────────

    def calculate_risk_metrics(self, returns: list[float]) -> dict:
        """
        İşlem getirilerinden risk metriklerini hesaplar.

        Args:
            returns: Yüzdesel getiri listesi (örn: [2.5, -1.0, 3.2, -0.5, ...])

        Returns:
            sharpe_ratio, sortino_ratio, var_95, max_drawdown, avg_return, volatility
        """
        if len(returns) < 3:
            return {"error": "Yeterli veri yok (min 3 işlem)", "sharpe": 0, "sortino": 0, "var_95": 0}

        n = len(returns)
        avg = sum(returns) / n
        risk_free = 0.05 / 252  # Yıllık %5 → günlük

        # Volatilite (standart sapma)
        variance = sum((r - avg) ** 2 for r in returns) / (n - 1)
        volatility = math.sqrt(variance)

        # Sharpe Ratio (yıllıklaştırılmış)
        sharpe = 0
        if volatility > 0:
            sharpe = round((avg - risk_free) / volatility * math.sqrt(252), 2)

        # Sortino Ratio (sadece negatif volatilite)
        downside_returns = [r for r in returns if r < 0]
        downside_vol = 0
        if len(downside_returns) > 1:
            down_var = sum((r - avg) ** 2 for r in downside_returns) / (len(downside_returns) - 1)
            downside_vol = math.sqrt(down_var)

        sortino = 0
        if downside_vol > 0:
            sortino = round((avg - risk_free) / downside_vol * math.sqrt(252), 2)

        # VaR %95 (Historical method)
        sorted_returns = sorted(returns)
        var_index = max(0, int(n * 0.05) - 1)
        var_95 = round(sorted_returns[var_index], 2)

        # Max Drawdown
        cumulative = 0
        peak = 0
        max_dd = 0
        for r in returns:
            cumulative += r
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)

        return {
            "sharpe": sharpe,
            "sortino": sortino,
            "var_95": var_95,
            "max_drawdown": round(max_dd, 2),
            "avg_return": round(avg, 4),
            "volatility": round(volatility, 4),
            "total_trades": n,
            "win_rate": round(len([r for r in returns if r > 0]) / n * 100, 1),
        }
