"""
strategy_optimizer.py — Strategy Optimizer with Grid Search (V5 Faz 5)

Backtest parametrelerini grid search ile otomatik optimize eder.
Her parametre kombinasyonunu backtest motorunda calistirip
en iyi Sharpe, en iyi getiri, en dusuk DD vb. hedeflere gore siralar.

Ciktilar:
  - Parametre grid sonuclari (her kombinasyonun metrikleri)
  - En iyi parametreler (hedef metrik bazli)
  - Heatmap verisi (2D parametre uzayi)
  - Overfitting uyarisi (in-sample vs out-of-sample karsilastirma)
"""

import itertools
from datetime import datetime, timezone


def optimize_strategy(
    ticker: str = "AAPL",
    days: int = 365,
    initial_capital: float = 100_000,
    target_metric: str = "sharpe_ratio",
    param_grid: dict | None = None,
) -> dict:
    """
    Grid search ile strateji parametrelerini optimize et.

    Args:
        ticker: Test edilecek hisse
        days: Backtest suresi
        initial_capital: Baslangic sermayesi
        target_metric: Optimize edilecek metrik
            (sharpe_ratio, total_return_pct, max_drawdown_pct, profit_factor, win_rate, calmar)
        param_grid: Parametre grid'i. None ise varsayilan kullanilir.

    Returns:
        dict: Optimizasyon sonuclari
    """
    from backtester import run_backtest

    # Varsayilan parametre grid'i
    if param_grid is None:
        param_grid = {
            "risk_per_trade": [0.01, 0.015, 0.02, 0.025, 0.03],
            "atr_sl_multiplier": [1.0, 1.5, 2.0, 2.5],
            "atr_tp_multiplier": [2.0, 2.5, 3.0, 4.0, 5.0],
            "min_momentum": [40, 50, 55, 60, 65, 70],
        }

    # Tum kombinasyonlari olustur
    keys = list(param_grid.keys())
    values = list(param_grid.values())
    combinations = list(itertools.product(*values))
    total = len(combinations)

    if total > 2000:
        return {"error": f"Cok fazla kombinasyon: {total}. Max 2000. Grid'i daralt."}

    results = []
    best_score = None
    best_params = None
    best_result = None

    # Metrik yonu (maximize veya minimize)
    minimize_metrics = {"max_drawdown_pct"}
    is_minimize = target_metric in minimize_metrics

    for i, combo in enumerate(combinations):
        params = dict(zip(keys, combo))

        try:
            bt = run_backtest(
                ticker=ticker,
                days=days,
                initial_capital=initial_capital,
                risk_per_trade=params.get("risk_per_trade", 0.02),
                atr_sl_multiplier=params.get("atr_sl_multiplier", 1.5),
                atr_tp_multiplier=params.get("atr_tp_multiplier", 3.0),
                min_momentum=params.get("min_momentum", 55),
            )

            if bt.get("error"):
                continue

            metrics = bt.get("metrics", {})
            score = metrics.get(target_metric, 0)

            # None veya NaN kontrol
            if score is None or (isinstance(score, float) and (score != score)):
                continue

            entry = {
                "params": params,
                "score": score,
                "total_return_pct": bt.get("total_return_pct", 0),
                "trade_count": metrics.get("total_trades", 0),
                "sharpe_ratio": metrics.get("sharpe_ratio", 0),
                "max_drawdown_pct": metrics.get("max_drawdown_pct", 0),
                "win_rate": metrics.get("win_rate", 0),
                "profit_factor": metrics.get("profit_factor", 0),
                "cagr": metrics.get("cagr", 0),
                "sortino_ratio": metrics.get("sortino_ratio", 0),
                "calmar": metrics.get("calmar", 0),
            }
            results.append(entry)

            # En iyi skoru guncelle
            if best_score is None:
                best_score = score
                best_params = params
                best_result = entry
            elif is_minimize and score < best_score:
                best_score = score
                best_params = params
                best_result = entry
            elif not is_minimize and score > best_score:
                best_score = score
                best_params = params
                best_result = entry

        except Exception as e:
            continue

    if not results:
        return {"error": "Hicbir kombinasyon basarili sonuc uretmedi"}

    # Sonuclari skora gore sirala
    results.sort(key=lambda x: x["score"], reverse=not is_minimize)

    # Top 10
    top_results = results[:10]

    # Heatmap verisi (2 parametre icin)
    heatmap = _build_heatmap(results, keys, target_metric)

    # Overfitting uyarisi
    overfitting_warning = _check_overfitting(results, target_metric)

    # Parametre hassasiyet analizi
    sensitivity = _sensitivity_analysis(results, keys, target_metric)

    return {
        "status": "ok",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "ticker": ticker,
        "days": days,
        "target_metric": target_metric,
        "total_combinations": total,
        "successful_runs": len(results),
        "best_params": best_params,
        "best_result": best_result,
        "top_results": top_results,
        "heatmap": heatmap,
        "overfitting_warning": overfitting_warning,
        "sensitivity": sensitivity,
    }


def _build_heatmap(results: list, keys: list, metric: str) -> dict:
    """SL vs TP icin heatmap verisi olustur."""
    if "atr_sl_multiplier" not in keys or "atr_tp_multiplier" not in keys:
        return {}

    heatmap = {}
    for r in results:
        sl = r["params"].get("atr_sl_multiplier", 0)
        tp = r["params"].get("atr_tp_multiplier", 0)
        key = f"{sl}x{tp}"
        if key not in heatmap:
            heatmap[key] = {"sl": sl, "tp": tp, "scores": []}
        heatmap[key]["scores"].append(r["score"])

    # Ortalama skoru hesapla
    for key in heatmap:
        scores = heatmap[key]["scores"]
        heatmap[key]["avg_score"] = round(sum(scores) / len(scores), 4) if scores else 0
        del heatmap[key]["scores"]

    return heatmap


def _check_overfitting(results: list, metric: str) -> str | None:
    """Overfitting uyarisi kontrol et."""
    if len(results) < 10:
        return None

    scores = [r["score"] for r in results]
    best = max(scores)
    avg = sum(scores) / len(scores)
    worst = min(scores)

    # En iyi vs ortalama farki cok buyukse overfitting riski var
    if avg != 0:
        spread = abs(best - avg) / abs(avg)
        if spread > 5:
            return "HIGH: Best result is >5x better than average. Likely overfitting."
        elif spread > 2:
            return "MODERATE: Significant variance between best and average results."

    # Cok az islem yapan parametreler uyarisi
    low_trade_count = sum(1 for r in results if r.get("trade_count", 0) < 5)
    if low_trade_count > len(results) * 0.5:
        return "WARNING: >50% of parameter sets produced fewer than 5 trades. Results may be unreliable."

    return None


def _sensitivity_analysis(results: list, keys: list, metric: str) -> dict:
    """Her parametrenin sonuca etkisini olc."""
    sensitivity = {}

    for key in keys:
        # Her parametre degeri icin ortalama skoru hesapla
        value_scores = {}
        for r in results:
            val = r["params"].get(key)
            if val not in value_scores:
                value_scores[val] = []
            value_scores[val].append(r["score"])

        # Ortalama ve standart sapma
        value_stats = {}
        for val, scores in value_scores.items():
            avg = sum(scores) / len(scores)
            value_stats[str(val)] = round(avg, 4)

        # Hassasiyet skoru: en iyi ve en kotu deger arasindaki fark
        if value_stats:
            vals = list(value_stats.values())
            impact = round(max(vals) - min(vals), 4) if len(vals) > 1 else 0
            sensitivity[key] = {
                "values": value_stats,
                "impact": impact,
            }

    # Impact'e gore sirala
    sensitivity = dict(sorted(sensitivity.items(), key=lambda x: x[1]["impact"], reverse=True))
    return sensitivity


def quick_optimize(ticker: str = "AAPL", days: int = 365) -> dict:
    """Hizli optimizasyon — kucuk grid ile."""
    return optimize_strategy(
        ticker=ticker,
        days=days,
        target_metric="sharpe_ratio",
        param_grid={
            "risk_per_trade": [0.01, 0.02, 0.03],
            "atr_sl_multiplier": [1.0, 1.5, 2.0],
            "atr_tp_multiplier": [2.5, 3.0, 4.0],
            "min_momentum": [50, 60, 70],
        },
    )
