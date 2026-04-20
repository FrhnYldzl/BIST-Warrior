"""
backtester.py — Strateji Backtesting Motoru (V3.2)

Mevcut sinyal mantığını (MACD, Bollinger, RSI, EMA, Volume) geçmiş veriye
uygulayarak stratejinin tarihsel performansını ölçer.

Çıktılar:
  - Equity curve (günlük)
  - İşlem listesi (entry/exit/P&L)
  - Performans metrikleri (Sharpe, MaxDD, Win Rate, Profit Factor, CAGR)
  - Benchmark karşılaştırması (SPY)
"""

import math
from datetime import datetime, timedelta, timezone

from market_scanner import (
    _ema, _rsi, _atr, _macd, _bollinger_bands, _detect_trend,
    _generate_signal, _calc_momentum_score,
)
from config import (
    BENCHMARK, LOOKBACK_DAYS,
    SIGNAL_GAP_THRESHOLD, SIGNAL_VOLUME_THRESHOLD,
    CONFIDENCE_RISK_MAP, ATR_MULTIPLIER,
)


# ─────────────────────────────────────────────────────────────────
# Veri çekme (Alpaca barları)
# ─────────────────────────────────────────────────────────────────

def _fetch_bars(ticker: str, days: int = 365) -> list[dict]:
    """Alpaca'dan günlük bar verisi çek. Her bar: {date, open, high, low, close, volume}"""
    import os
    from pathlib import Path
    from dotenv import load_dotenv, dotenv_values

    _env_path = Path(__file__).parent.parent / ".env"
    load_dotenv(_env_path)
    _env_vals = dotenv_values(_env_path)
    def _get(key): return os.getenv(key) or _env_vals.get(key, "")

    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    client = StockHistoricalDataClient(
        api_key=_get("ALPACA_API_KEY"),
        secret_key=_get("ALPACA_SECRET_KEY"),
    )

    end = datetime.now(timezone.utc) - timedelta(days=1)
    start = end - timedelta(days=days)

    request = StockBarsRequest(
        symbol_or_symbols=ticker,
        timeframe=TimeFrame.Day,
        start=start,
        end=end,
    )

    barset = client.get_stock_bars(request)
    bars = []
    for bar in barset[ticker]:
        bars.append({
            "date": bar.timestamp.strftime("%Y-%m-%d"),
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": int(bar.volume),
        })
    return bars


# ─────────────────────────────────────────────────────────────────
# Teknik analiz hesaplama (bar listesinden)
# ─────────────────────────────────────────────────────────────────

def _compute_indicators(bars: list[dict], idx: int) -> dict | None:
    """
    bars[0..idx] aralığındaki veriden teknik indikatör hesapla.
    En az 50 bar gerekli (EMA50 için).
    """
    if idx < 50:
        return None

    window = bars[:idx + 1]
    closes = [b["close"] for b in window]
    highs = [b["high"] for b in window]
    lows = [b["low"] for b in window]
    volumes = [b["volume"] for b in window]

    price = closes[-1]
    prev_close = closes[-2] if len(closes) > 1 else price

    ema9 = _ema(closes, 9)
    ema21 = _ema(closes, 21)
    ema50 = _ema(closes, 50)
    rsi = _rsi(closes, 14)
    atr = _atr(highs, lows, closes, 14)
    macd_data = _macd(closes)
    bb_data = _bollinger_bands(closes)
    trend = _detect_trend(closes, ema9, ema21, ema50)

    change_pct = ((price - prev_close) / prev_close * 100) if prev_close else 0

    # Volume ratio (20-day avg)
    vol_avg_20 = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else sum(volumes) / max(len(volumes), 1)
    vol_ratio = volumes[-1] / vol_avg_20 if vol_avg_20 > 0 else 1.0

    # Gap %
    open_price = bars[idx]["open"]
    gap_pct = ((open_price - prev_close) / prev_close * 100) if prev_close else 0

    atr_pct = (atr / price * 100) if price > 0 else 0

    signal = _generate_signal(
        ema9, ema21, ema50, rsi, vol_ratio, change_pct, gap_pct, trend,
        macd_data=macd_data, bb_data=bb_data, current_price=price,
    )

    momentum = _calc_momentum_score(
        change_pct, gap_pct, vol_ratio, rsi, ema9, ema21, ema50, atr_pct, trend,
        macd_data=macd_data, bb_data=bb_data, current_price=price,
    )

    return {
        "price": price,
        "atr": atr,
        "rsi": rsi,
        "ema9": ema9,
        "ema21": ema21,
        "ema50": ema50,
        "macd": macd_data,
        "bb": bb_data,
        "trend": trend,
        "signal": signal,
        "momentum": momentum,
        "vol_ratio": vol_ratio,
        "change_pct": change_pct,
        "gap_pct": gap_pct,
    }


# ─────────────────────────────────────────────────────────────────
# Backtest motoru
# ─────────────────────────────────────────────────────────────────

def run_backtest(
    ticker: str,
    days: int = 365,
    initial_capital: float = 100_000,
    risk_per_trade: float = 0.02,
    atr_sl_multiplier: float = 1.5,
    atr_tp_multiplier: float = 3.0,
    min_momentum: int = 55,
) -> dict:
    """
    Tek hisse için backtest çalıştır.

    Strateji:
    - signal == strong_buy/buy VE momentum >= min_momentum → LONG giriş
    - Stop-loss: entry - ATR * atr_sl_multiplier
    - Take-profit: entry + ATR * atr_tp_multiplier
    - signal == sell/strong_sell → pozisyon kapat
    - Aynı anda max 1 pozisyon

    Returns:
        {ticker, period, trades, equity_curve, metrics, benchmark}
    """
    bars = _fetch_bars(ticker, days)
    if len(bars) < 60:
        return {"error": f"Yetersiz veri: {len(bars)} bar (min 60 gerekli)"}

    # Benchmark verisi
    try:
        spy_bars = _fetch_bars(BENCHMARK, days)
    except Exception:
        spy_bars = []

    capital = initial_capital
    position = None  # {entry_price, qty, stop_loss, take_profit, entry_date, entry_idx}
    trades = []
    equity_curve = []

    for i in range(50, len(bars)):
        bar = bars[i]
        indicators = _compute_indicators(bars, i)
        if not indicators:
            continue

        price = indicators["price"]
        signal = indicators["signal"]
        momentum = indicators["momentum"]
        atr = indicators["atr"]

        # Günlük equity hesapla
        unrealized = 0
        if position:
            unrealized = (price - position["entry_price"]) * position["qty"]

        equity_curve.append({
            "date": bar["date"],
            "equity": round(capital + unrealized, 2),
            "price": price,
        })

        # Pozisyon varsa: stop/tp/signal kontrol
        if position:
            # Stop-loss hit
            if bar["low"] <= position["stop_loss"]:
                exit_price = position["stop_loss"]
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                capital += pnl + position["entry_price"] * position["qty"]
                trades.append({
                    "entry_date": position["entry_date"],
                    "exit_date": bar["date"],
                    "entry_price": position["entry_price"],
                    "exit_price": round(exit_price, 2),
                    "qty": position["qty"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (position["entry_price"] * position["qty"]) * 100, 2),
                    "exit_reason": "stop_loss",
                    "holding_days": i - position["entry_idx"],
                })
                position = None
                continue

            # Take-profit hit
            if bar["high"] >= position["take_profit"]:
                exit_price = position["take_profit"]
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                capital += pnl + position["entry_price"] * position["qty"]
                trades.append({
                    "entry_date": position["entry_date"],
                    "exit_date": bar["date"],
                    "entry_price": position["entry_price"],
                    "exit_price": round(exit_price, 2),
                    "qty": position["qty"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (position["entry_price"] * position["qty"]) * 100, 2),
                    "exit_reason": "take_profit",
                    "holding_days": i - position["entry_idx"],
                })
                position = None
                continue

            # Signal-based exit
            if signal in ("sell", "strong_sell"):
                exit_price = price
                pnl = (exit_price - position["entry_price"]) * position["qty"]
                capital += pnl + position["entry_price"] * position["qty"]
                trades.append({
                    "entry_date": position["entry_date"],
                    "exit_date": bar["date"],
                    "entry_price": position["entry_price"],
                    "exit_price": round(exit_price, 2),
                    "qty": position["qty"],
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl / (position["entry_price"] * position["qty"]) * 100, 2),
                    "exit_reason": f"signal_{signal}",
                    "holding_days": i - position["entry_idx"],
                })
                position = None
                continue

        # Pozisyon yoksa: giriş sinyali ara
        if position is None and signal in ("strong_buy", "buy") and momentum >= min_momentum:
            if atr <= 0:
                continue
            stop_loss = price - atr * atr_sl_multiplier
            take_profit = price + atr * atr_tp_multiplier
            risk_amount = capital * risk_per_trade
            risk_per_share = price - stop_loss
            if risk_per_share <= 0:
                continue
            qty = int(risk_amount / risk_per_share)
            if qty <= 0:
                continue
            cost = qty * price
            if cost > capital:
                qty = int(capital / price)
                if qty <= 0:
                    continue
                cost = qty * price

            capital -= cost
            position = {
                "entry_price": price,
                "qty": qty,
                "stop_loss": round(stop_loss, 2),
                "take_profit": round(take_profit, 2),
                "entry_date": bar["date"],
                "entry_idx": i,
            }

    # Açık pozisyonu kapat (son bar)
    if position:
        final_price = bars[-1]["close"]
        pnl = (final_price - position["entry_price"]) * position["qty"]
        capital += pnl + position["entry_price"] * position["qty"]
        trades.append({
            "entry_date": position["entry_date"],
            "exit_date": bars[-1]["date"],
            "entry_price": position["entry_price"],
            "exit_price": round(final_price, 2),
            "qty": position["qty"],
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl / (position["entry_price"] * position["qty"]) * 100, 2),
            "exit_reason": "end_of_period",
            "holding_days": len(bars) - 1 - position["entry_idx"],
        })
        position = None

    # Metrikleri hesapla
    metrics = _calc_metrics(trades, equity_curve, initial_capital)

    # Benchmark performansı
    benchmark = {}
    if spy_bars and len(spy_bars) > 1:
        spy_start = spy_bars[0]["close"]
        spy_end = spy_bars[-1]["close"]
        benchmark = {
            "ticker": BENCHMARK,
            "return_pct": round((spy_end - spy_start) / spy_start * 100, 2),
            "start_price": spy_start,
            "end_price": spy_end,
        }

    return {
        "ticker": ticker,
        "period_days": days,
        "bar_count": len(bars),
        "initial_capital": initial_capital,
        "final_equity": round(capital, 2),
        "total_return_pct": round((capital - initial_capital) / initial_capital * 100, 2),
        "trades": trades,
        "trade_count": len(trades),
        "equity_curve": equity_curve,
        "metrics": metrics,
        "benchmark": benchmark,
        "parameters": {
            "risk_per_trade": risk_per_trade,
            "atr_sl_multiplier": atr_sl_multiplier,
            "atr_tp_multiplier": atr_tp_multiplier,
            "min_momentum": min_momentum,
        },
    }


# ─────────────────────────────────────────────────────────────────
# Performans metrikleri
# ─────────────────────────────────────────────────────────────────

def _calc_metrics(trades: list, equity_curve: list, initial_capital: float) -> dict:
    """Backtest performans metrikleri."""
    if not trades:
        return {
            "sharpe_ratio": 0, "sortino_ratio": 0, "max_drawdown_pct": 0,
            "win_rate": 0, "profit_factor": 0, "avg_win": 0, "avg_loss": 0,
            "best_trade": 0, "worst_trade": 0, "avg_holding_days": 0,
            "total_trades": 0, "cagr": 0, "calmar_ratio": 0,
        }

    pnls = [t["pnl"] for t in trades]
    pnl_pcts = [t["pnl_pct"] for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    win_rate = len(wins) / len(trades) * 100
    avg_win = sum(wins) / len(wins) if wins else 0
    avg_loss = sum(losses) / len(losses) if losses else 0
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf") if gross_profit > 0 else 0

    # Max Drawdown
    peak = initial_capital
    max_dd = 0
    for point in equity_curve:
        eq = point["equity"]
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak * 100
        if dd > max_dd:
            max_dd = dd

    # Sharpe Ratio (günlük getiriden yıllık)
    if len(equity_curve) > 1:
        daily_returns = []
        for j in range(1, len(equity_curve)):
            prev_eq = equity_curve[j - 1]["equity"]
            curr_eq = equity_curve[j]["equity"]
            if prev_eq > 0:
                daily_returns.append((curr_eq - prev_eq) / prev_eq)

        if daily_returns:
            mean_r = sum(daily_returns) / len(daily_returns)
            std_r = math.sqrt(sum((r - mean_r) ** 2 for r in daily_returns) / len(daily_returns))
            sharpe = (mean_r / std_r * math.sqrt(252)) if std_r > 0 else 0

            # Sortino (sadece negatif volatilite)
            neg_returns = [r for r in daily_returns if r < 0]
            if neg_returns:
                downside_std = math.sqrt(sum(r ** 2 for r in neg_returns) / len(neg_returns))
                sortino = (mean_r / downside_std * math.sqrt(252)) if downside_std > 0 else 0
            else:
                sortino = sharpe * 1.5  # Negatif yok = harika
        else:
            sharpe = sortino = 0
    else:
        sharpe = sortino = 0

    # CAGR
    if equity_curve:
        final_eq = equity_curve[-1]["equity"]
        n_days = len(equity_curve)
        years = n_days / 252
        cagr = ((final_eq / initial_capital) ** (1 / years) - 1) * 100 if years > 0 and initial_capital > 0 else 0
    else:
        cagr = 0

    # Calmar Ratio (CAGR / MaxDD)
    calmar = cagr / max_dd if max_dd > 0 else 0

    holding_days = [t["holding_days"] for t in trades]

    return {
        "total_trades": len(trades),
        "win_rate": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "sharpe_ratio": round(sharpe, 2),
        "sortino_ratio": round(sortino, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "cagr": round(cagr, 2),
        "calmar_ratio": round(calmar, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "best_trade": round(max(pnls), 2),
        "worst_trade": round(min(pnls), 2),
        "avg_holding_days": round(sum(holding_days) / len(holding_days), 1),
    }


# ─────────────────────────────────────────────────────────────────
# Multi-ticker backtest
# ─────────────────────────────────────────────────────────────────

def run_portfolio_backtest(
    tickers: list[str],
    days: int = 365,
    initial_capital: float = 100_000,
    **kwargs,
) -> dict:
    """
    Birden fazla hisse için backtest çalıştır ve portföy seviyesinde özetle.
    """
    results = {}
    total_pnl = 0
    all_trades = []
    capital_per_ticker = initial_capital / len(tickers) if tickers else initial_capital

    for ticker in tickers:
        try:
            r = run_backtest(ticker, days=days, initial_capital=capital_per_ticker, **kwargs)
            results[ticker] = r
            if "error" not in r:
                total_pnl += r["final_equity"] - capital_per_ticker
                for t in r.get("trades", []):
                    t["ticker"] = ticker
                    all_trades.append(t)
        except Exception as e:
            results[ticker] = {"error": str(e)}

    # Tüm işlemleri tarihe göre sırala
    all_trades.sort(key=lambda t: t.get("entry_date", ""))

    final_equity = initial_capital + total_pnl
    return {
        "tickers": tickers,
        "period_days": days,
        "initial_capital": initial_capital,
        "final_equity": round(final_equity, 2),
        "total_return_pct": round(total_pnl / initial_capital * 100, 2),
        "per_ticker": results,
        "all_trades": all_trades,
        "total_trade_count": len(all_trades),
    }
