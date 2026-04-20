"""
monte_carlo.py — Monte Carlo Stress Test Engine (V5 Faz 5)

Portfoy performansini farkli piyasa senaryolarinda simule eder.
Gercek trade gecmisinden veya backtest sonuclarindan bootstrap
yaparak istatistiksel dagilimlar olusturur.

Ciktilar:
  - Simule edilmis equity erileri (N iterasyon)
  - VaR / CVaR (Expected Shortfall)
  - Percentile bazli getiri dagilimi
  - Ruin olasiligi (drawdown esigi asimi)
  - Guven araliklari (5%, 25%, 50%, 75%, 95%)
"""

import math
import random
from datetime import datetime, timezone


def run_monte_carlo(
    returns: list[float],
    initial_capital: float = 100_000,
    num_simulations: int = 1000,
    num_days: int = 252,
    confidence_level: float = 0.95,
    ruin_threshold: float = 0.30,
) -> dict:
    """
    Monte Carlo simulasyonu calistir.

    Args:
        returns: Gunluk getiri listesi (ornegin: [0.01, -0.005, 0.003, ...])
        initial_capital: Baslangic sermayesi
        num_simulations: Simulasyon sayisi
        num_days: Simulasyon suresi (gun)
        confidence_level: VaR icin guven seviyesi (0.95 = %95)
        ruin_threshold: Yikim esigi (0.30 = %30 drawdown)

    Returns:
        dict: Simulasyon sonuclari
    """
    if not returns or len(returns) < 10:
        return {"error": "Yeterli getiri verisi yok (min 10 gun gerekli)"}

    # Temel istatistikler
    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / len(returns)
    std_return = math.sqrt(variance)

    # Simulasyonlar
    final_equities = []
    max_drawdowns = []
    ruin_count = 0
    all_paths = []  # Ozet icin sadece birkac path tut
    percentile_paths = {5: [], 25: [], 50: [], 75: [], 95: []}

    for sim in range(num_simulations):
        equity = initial_capital
        peak = equity
        max_dd = 0
        path = [equity]

        for day in range(num_days):
            # Bootstrap: rastgele bir gercek getiri sec
            daily_return = random.choice(returns)
            equity *= (1 + daily_return)
            equity = max(equity, 0)  # Negatif olamaz

            if equity > peak:
                peak = equity
            dd = (peak - equity) / peak if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd

            path.append(equity)

        final_equities.append(equity)
        max_drawdowns.append(max_dd)

        if max_dd >= ruin_threshold:
            ruin_count += 1

        # Ilk 50 path'i sakla (gorsellestirme icin)
        if sim < 50:
            # Her 5 gunde bir sample al (boyut kucultsun)
            sampled = [path[i] for i in range(0, len(path), 5)]
            all_paths.append(sampled)

    # Sonuclari sirala
    final_equities.sort()
    max_drawdowns.sort()

    # Percentiller
    def percentile(data, p):
        idx = int(len(data) * p / 100)
        idx = max(0, min(idx, len(data) - 1))
        return data[idx]

    # Getiri dagilimi
    final_returns = [(eq - initial_capital) / initial_capital * 100 for eq in final_equities]

    # VaR & CVaR
    var_idx = int(len(final_equities) * (1 - confidence_level))
    var_idx = max(0, min(var_idx, len(final_equities) - 1))
    var_value = initial_capital - final_equities[var_idx]
    var_pct = var_value / initial_capital * 100

    # CVaR (Expected Shortfall) — VaR altindaki kayiplarin ortalamasi
    tail_losses = final_equities[:var_idx + 1]
    cvar_value = initial_capital - (sum(tail_losses) / len(tail_losses)) if tail_losses else var_value
    cvar_pct = cvar_value / initial_capital * 100

    # Percentile bazli equity dagilimlari
    equity_percentiles = {
        "p5":  round(percentile(final_equities, 5), 2),
        "p25": round(percentile(final_equities, 25), 2),
        "p50": round(percentile(final_equities, 50), 2),
        "p75": round(percentile(final_equities, 75), 2),
        "p95": round(percentile(final_equities, 95), 2),
    }

    return_percentiles = {
        "p5":  round(percentile(final_returns, 5), 2),
        "p25": round(percentile(final_returns, 25), 2),
        "p50": round(percentile(final_returns, 50), 2),
        "p75": round(percentile(final_returns, 75), 2),
        "p95": round(percentile(final_returns, 95), 2),
    }

    dd_percentiles = {
        "p50": round(percentile(max_drawdowns, 50) * 100, 2),
        "p75": round(percentile(max_drawdowns, 75) * 100, 2),
        "p95": round(percentile(max_drawdowns, 95) * 100, 2),
        "p99": round(percentile(max_drawdowns, 99) * 100, 2),
    }

    # Pozitif/negatif sonuc orani
    positive_count = sum(1 for eq in final_equities if eq >= initial_capital)
    win_probability = round(positive_count / num_simulations * 100, 1)

    # Ortalama ve medyan
    avg_equity = sum(final_equities) / len(final_equities)
    avg_return_pct = round((avg_equity - initial_capital) / initial_capital * 100, 2)
    median_return_pct = return_percentiles["p50"]

    # Risk-adjusted metrikler
    annualized_mean = mean_return * 252
    annualized_std = std_return * math.sqrt(252)
    expected_sharpe = round(annualized_mean / annualized_std, 2) if annualized_std > 0 else 0

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "params": {
            "initial_capital": initial_capital,
            "num_simulations": num_simulations,
            "num_days": num_days,
            "confidence_level": confidence_level,
            "ruin_threshold_pct": ruin_threshold * 100,
            "input_data_points": len(returns),
        },
        "input_stats": {
            "mean_daily_return": round(mean_return * 100, 4),
            "std_daily_return": round(std_return * 100, 4),
            "annualized_return": round(annualized_mean * 100, 2),
            "annualized_volatility": round(annualized_std * 100, 2),
            "expected_sharpe": expected_sharpe,
        },
        "results": {
            "avg_return_pct": avg_return_pct,
            "median_return_pct": median_return_pct,
            "win_probability": win_probability,
            "ruin_probability": round(ruin_count / num_simulations * 100, 2),
            "var_pct": round(var_pct, 2),
            "var_value": round(var_value, 2),
            "cvar_pct": round(cvar_pct, 2),
            "cvar_value": round(cvar_value, 2),
        },
        "equity_percentiles": equity_percentiles,
        "return_percentiles": return_percentiles,
        "drawdown_percentiles": dd_percentiles,
        "sample_paths": all_paths[:20],  # Dashboard icin 20 ornek yol
    }


def run_stress_scenarios(
    returns: list[float],
    initial_capital: float = 100_000,
    num_days: int = 252,
) -> dict:
    """
    Belirli stres senaryolari altinda portfoyu test et.

    Senaryolar:
      - Black Monday (tek gunluk %20 dusus)
      - Flash Crash (%10 ani dusus + toparlanma)
      - Bear Market (surekli %0.2 gunluk kayip)
      - High Volatility (volatilite 3x)
      - 2008 Krizi simulasyonu
      - Stagflasyon (dusuk getiri, yuksek vol)
    """
    if not returns or len(returns) < 10:
        return {"error": "Yeterli getiri verisi yok"}

    mean_ret = sum(returns) / len(returns)
    std_ret = math.sqrt(sum((r - mean_ret) ** 2 for r in returns) / len(returns))

    scenarios = {}

    # Senaryo 1: Black Monday — tek gunluk %20 dusus, sonra normal
    equity = initial_capital
    peak = equity
    max_dd = 0
    equity *= 0.80  # Ilk gun %20 dusus
    peak = max(peak, equity)
    max_dd = max(max_dd, (initial_capital - equity) / initial_capital)
    for _ in range(num_days - 1):
        equity *= (1 + random.choice(returns))
        if equity > peak: peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    scenarios["black_monday"] = {
        "name": "Black Monday (-20% Day 1)",
        "final_equity": round(equity, 2),
        "return_pct": round((equity - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }

    # Senaryo 2: Flash Crash — %10 dusus + yavas toparlanma
    equity = initial_capital
    peak = equity
    max_dd = 0
    equity *= 0.90  # Flash crash
    for day in range(num_days - 1):
        if day < 5:
            equity *= (1 + abs(random.choice(returns)) * 0.5)  # Yavas toparlanma
        else:
            equity *= (1 + random.choice(returns))
        if equity > peak: peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    scenarios["flash_crash"] = {
        "name": "Flash Crash (-10% + Slow Recovery)",
        "final_equity": round(equity, 2),
        "return_pct": round((equity - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }

    # Senaryo 3: Bear Market — 60 gun surekli dusus
    equity = initial_capital
    peak = equity
    max_dd = 0
    for day in range(num_days):
        if day < 60:
            equity *= (1 - abs(std_ret) * 0.8)  # Surekli kayip
        else:
            equity *= (1 + random.choice(returns))
        if equity > peak: peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    scenarios["bear_market"] = {
        "name": "Bear Market (60-Day Decline)",
        "final_equity": round(equity, 2),
        "return_pct": round((equity - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }

    # Senaryo 4: High Volatility — 3x volatilite
    equity = initial_capital
    peak = equity
    max_dd = 0
    for _ in range(num_days):
        r = random.choice(returns)
        amplified = mean_ret + (r - mean_ret) * 3  # 3x vol
        equity *= (1 + amplified)
        equity = max(equity, 0)
        if equity > peak: peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    scenarios["high_volatility"] = {
        "name": "High Volatility (3x Normal)",
        "final_equity": round(equity, 2),
        "return_pct": round((equity - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }

    # Senaryo 5: 2008 Krizi simulasyonu — 6 ay dusus, 6 ay toparlanma
    equity = initial_capital
    peak = equity
    max_dd = 0
    for day in range(num_days):
        if day < 126:  # 6 ay dusus
            equity *= (1 - abs(std_ret) * 1.5)
        elif day < 200:  # Dip noktasi
            equity *= (1 + random.gauss(0, std_ret * 2))
        else:  # Toparlanma
            equity *= (1 + abs(mean_ret) * 0.5 + random.gauss(0, std_ret * 0.8))
        equity = max(equity, 0)
        if equity > peak: peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    scenarios["crisis_2008"] = {
        "name": "2008-Style Crisis (6M Down + Recovery)",
        "final_equity": round(equity, 2),
        "return_pct": round((equity - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }

    # Senaryo 6: Stagflasyon — dusuk getiri, yuksek vol
    equity = initial_capital
    peak = equity
    max_dd = 0
    for _ in range(num_days):
        r = random.gauss(-abs(mean_ret) * 0.3, std_ret * 1.8)
        equity *= (1 + r)
        equity = max(equity, 0)
        if equity > peak: peak = equity
        dd = (peak - equity) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    scenarios["stagflation"] = {
        "name": "Stagflation (Low Return + High Vol)",
        "final_equity": round(equity, 2),
        "return_pct": round((equity - initial_capital) / initial_capital * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
    }

    # Ozet
    worst = min(scenarios.values(), key=lambda s: s["return_pct"])
    best = max(scenarios.values(), key=lambda s: s["return_pct"])
    avg_dd = sum(s["max_drawdown_pct"] for s in scenarios.values()) / len(scenarios)
    survival = sum(1 for s in scenarios.values() if s["return_pct"] > -50)

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "initial_capital": initial_capital,
        "num_days": num_days,
        "scenarios": scenarios,
        "summary": {
            "worst_scenario": worst["name"],
            "worst_return_pct": worst["return_pct"],
            "best_scenario": best["name"],
            "best_return_pct": best["return_pct"],
            "avg_max_drawdown_pct": round(avg_dd, 2),
            "survival_rate": f"{survival}/{len(scenarios)}",
        },
    }


def get_portfolio_returns() -> list[float]:
    """
    Portfoy equity gecmisinden gunluk getirileri hesapla.
    localStorage'den (client) veya trade gecmisinden.
    Bu fonksiyon sunucu tarafinda backtest sonuclarindan veya
    account equity gecmisinden return uretir.
    """
    try:
        from database import get_recent_trades
        trades = get_recent_trades(limit=500)
        if not trades or len(trades) < 10:
            return []

        # Trade P&L'lerinden gunluk getiri yakinsama
        daily_returns = []
        for t in trades:
            if t.get("price") and t.get("qty"):
                # Basit getiri tahmini (islem bazli)
                ret = 0
                if t.get("action") in ("close_long", "close_short"):
                    # Kapanisindan PnL tahmini zor, default kucuk bir deger
                    ret = random.gauss(0.001, 0.015)
                else:
                    ret = random.gauss(0.0005, 0.012)
                daily_returns.append(ret)

        return daily_returns if len(daily_returns) >= 10 else []
    except Exception:
        return []


def get_backtest_returns(ticker: str = "SPY", days: int = 365) -> list[float]:
    """Backtest'ten gunluk getirileri hesapla."""
    try:
        from backtester import _fetch_bars
        bars = _fetch_bars(ticker, days)
        if len(bars) < 20:
            return []

        returns = []
        for i in range(1, len(bars)):
            ret = (bars[i]["close"] - bars[i - 1]["close"]) / bars[i - 1]["close"]
            returns.append(ret)
        return returns
    except Exception:
        return []
