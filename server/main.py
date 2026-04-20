"""
main.py — AI Trading Agent V2 Sunucusu

Artik sadece bir webhook sunucu degil — otonom bir trading agent.

Katmanlar:
  1. Data Layer   : Alpaca Data API (market_scanner.py)
  2. Brain Layer  : Claude AI (claude_brain.py) — rejim + strateji + multi-step reasoning
  3. Risk Layer   : Dinamik pozisyon boyutlandirma (risk_manager.py)
  4. Execution    : Alpaca Trading API (broker/equity.py)
  5. Storage      : SQLite (database.py) + post-trade review

Baslatmak icin:
    uvicorn main:app --host 0.0.0.0 --port 8000 --reload

Dashboard:
    http://localhost:8000
"""

import asyncio
import json
import os
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from broker import EquityBroker  # BROKER env var ile yfinance/ibkr seçilir
from database import get_recent_trades, init_db, log_trade, clear_old_trades
from trade_journal import (
    init_journal_db, log_journal_entry, get_journal_entries,
    calculate_performance, generate_lesson, get_learning_context,
)
from risk_manager import RiskManager
from ai_advisor import analyze_trade, review_strategy, is_enabled
import scheduler as sched
from market_scanner import get_market_data, get_multi_timeframe, get_correlation_matrix
from config import WATCHLIST
from universe_loader import get_universe, get_universe_info
from backtester import run_backtest, run_portfolio_backtest
from regime_detector import detect_regime
from news_sentiment import get_market_sentiment, get_ticker_sentiment
from anomaly_detector import detect_anomalies
from gemini_auditor import get_last_audit, is_enabled as gemini_enabled
from notifier import send_trade_notification, is_enabled as notify_enabled
from monte_carlo import run_monte_carlo, run_stress_scenarios, get_backtest_returns
from strategy_optimizer import optimize_strategy, quick_optimize
from trade_journal_v2 import (
    init_journal_v2, log_trade_v2, get_journal_v2,
    get_journal_analytics, export_journal_csv,
)
import midas_journal as midas
import config as cfg

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)

# ──────────────────────────────────────────────────────────────────
# WebSocket baglanti yöneticisi (canli bildirimler icin)
# ──────────────────────────────────────────────────────────────────

class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_text(json.dumps(data))
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.active.remove(ws)

manager = ConnectionManager()

# ──────────────────────────────────────────────────────────────────
# Uygulama baslatma
# ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    init_journal_v2()
    midas.init_midas_db()
    # Meridian Capital TR — analyst modunda AUTO_EXECUTE=False
    auto_exec = cfg.AUTO_EXECUTE and cfg.OPERATION_MODE != "analyst"
    sched.start(broker=broker, auto_execute=auto_exec, interval_minutes=10)
    mode_str = "📱 ANALYST (Midas elle emir)" if cfg.OPERATION_MODE == "analyst" else "🤖 AUTO-EXEC"
    print(f"⚔️  BIST WARRIOR with MIDAS — BIST AI Analist baslatildi ({mode_str}) >> http://localhost:{cfg.PORT}")
    yield
    sched.stop()

app = FastAPI(
    title="BIST Warrior with MIDAS",
    description="BIST AI Analist · Midas Destekli Manuel Trading Terminal",
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/api/docs",
)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ──────────────────────────────────────────────────────────────────
# Sabitler
# ──────────────────────────────────────────────────────────────────

WEBHOOK_SECRET = cfg.WEBHOOK_SECRET
MAX_RISK_PCT = cfg.MAX_RISK_PCT
AI_APPROVAL_REQUIRED = cfg.AI_APPROVAL_REQUIRED

broker = EquityBroker()
risk = RiskManager(max_risk_pct=MAX_RISK_PCT)

# ──────────────────────────────────────────────────────────────────
# Veri Modelleri
# ──────────────────────────────────────────────────────────────────

class Signal(BaseModel):
    ticker: str = Field(..., examples=["AKBNK.IS"])
    action: str = Field(..., examples=["long"])
    price: float = Field(..., gt=0)
    qty: float | None = Field(None, gt=0)
    secret: str | None = None

# ──────────────────────────────────────────────────────────────────
# Dashboard (kök sayfa)
# ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard():
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )

# ──────────────────────────────────────────────────────────────────
# WebSocket — canli islem bildirimleri
# ──────────────────────────────────────────────────────────────────

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

# ──────────────────────────────────────────────────────────────────
# API Endpoint'leri
# ──────────────────────────────────────────────────────────────────

@app.post("/webhook")
async def handle_webhook(signal: Signal):
    if WEBHOOK_SECRET and signal.secret != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Gecersiz webhook secret.")

    loop = asyncio.get_event_loop()

    if signal.qty:
        qty = signal.qty
    else:
        balance = await loop.run_in_executor(None, broker.get_balance)
        qty = risk.calculate_position_size(
            balance=balance,
            entry_price=signal.price,
            stop_loss_pct=0.02,
        )

    # Claude AI Analizi
    recent = get_recent_trades(limit=5)
    balance_for_ai = await loop.run_in_executor(None, broker.get_balance)
    ai_analysis = await loop.run_in_executor(
        None,
        partial(analyze_trade,
            ticker=signal.ticker,
            action=signal.action,
            price=signal.price,
            qty=qty,
            balance=balance_for_ai,
            recent_trades=recent,
        )
    )

    # AI onay zorunluysa ve reddedildiyse dur
    if AI_APPROVAL_REQUIRED and not ai_analysis.get("approved", True):
        raise HTTPException(
            status_code=400,
            detail=f"AI Advisor reddetti: {ai_analysis.get('reasoning')}"
        )

    # Islemi gerceklestir (V2.1: guvenlik kontrolleri broker icinde)
    try:
        result = await loop.run_in_executor(
            None,
            partial(broker.execute,
                action=signal.action,
                ticker=signal.ticker,
                qty=qty,
                price=signal.price,
            )
        )
    except Exception as e:
        err = {"status": "error", "message": str(e)}
        signal.qty = qty
        log_trade(signal, err)
        raise HTTPException(status_code=400, detail=str(e))

    # V2.1: Broker rejected kontrolu (loop/market/price kontrolleri)
    if result.get("status") == "rejected":
        signal.qty = qty
        log_trade(signal, result)
        raise HTTPException(
            status_code=400,
            detail=f"Emir reddedildi: {result.get('reason', 'Bilinmeyen neden')}"
        )

    signal.qty = qty
    log_trade(signal, result)

    # Dashboard'a canli bildirim + AI yorumu gönder
    await manager.broadcast({
        "type": "new_trade",
        "ticker": signal.ticker,
        "action": signal.action,
        "qty": qty,
        "price": signal.price,
        "status": result.get("status"),
        "ai": ai_analysis,
    })

    return {
        "status": "ok",
        "ticker": signal.ticker,
        "action": signal.action,
        "qty": qty,
        "result": result,
        "ai_analysis": ai_analysis,
    }


@app.get("/api/trades")
async def list_trades(limit: int = 50):
    return get_recent_trades(limit=limit)


@app.get("/api/account")
async def get_account():
    """BIST broker hesap bilgisi — equity.py / ibkr.py ortak arayüzü üzerinden."""
    try:
        acc = broker.get_account_status()
        if "error" in acc:
            raise Exception(acc["error"])
        positions = broker.get_all_positions()
        return {
            "cash": acc.get("cash", 0),
            "portfolio_value": acc.get("portfolio_value", 0),
            "equity": acc.get("equity", 0),
            "buying_power": acc.get("buying_power", acc.get("cash", 0)),
            "total_return_pct": acc.get("total_return_pct", 0),
            "currency": acc.get("currency", "TRY"),
            "has_pending": False,  # BIST paper: pending emir tutmuyor
            "positions": [
                {
                    "ticker": p.get("ticker", p.get("symbol", "")),
                    "qty": p.get("qty", 0),
                    "avg_entry": p.get("avg_entry_price", 0),
                    "current_price": p.get("current_price", 0),
                    "unrealized_pl": p.get("unrealized_pl", 0),
                    "unrealized_plpc": p.get("unrealized_plpc", 0),
                }
                for p in positions
            ],
        }
    except Exception as e:
        return {"error": str(e), "cash": 0, "portfolio_value": 0, "equity": 0,
                "buying_power": 0, "has_pending": False, "positions": [], "currency": "TRY"}


@app.get("/api/recommendations")
async def get_recommendations():
    """Son Claude tarama kararlarini döndürür — V2: rejim + strateji + güven skoru."""
    return sched.get_last_scan()


@app.post("/api/scan-now")
async def trigger_scan():
    """Manuel tarama baslat (dashboard'dan tetiklenebilir)."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, lambda: sched.run_scan(broker=broker, auto_execute=False)
    )
    return sched.get_last_scan()


@app.get("/api/watchlist")
async def get_watchlist():
    return {"watchlist": WATCHLIST}


@app.get("/api/universe")
async def get_universe_endpoint():
    """BIST evren bilgisi — wide-scan için tüm hisseler + sektörler."""
    return get_universe_info()


@app.get("/api/strategy-review")
async def strategy_review():
    """Tüm islem gecmisini analiz edip strateji önerileri üretir."""
    trades = get_recent_trades(limit=50)
    return review_strategy(trades)


@app.get("/api/post-trade-review")
async def post_trade_review():
    """Son post-trade review sonucunu döndürür."""
    return sched.get_last_review()


@app.post("/api/run-review")
async def trigger_review():
    """Manuel post-trade review tetikle."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None, lambda: sched.run_review(broker=broker)
    )
    return sched.get_last_review()


@app.get("/api/risk-check")
async def risk_check():
    """Portföy risk analizi — rejime göre uyumluluk kontrolü."""
    try:
        acc = broker.get_account_status()
        positions = broker.get_all_positions()
        equity = float(acc.get("equity", 0))
        pos_list = [
            {
                "ticker": p.get("ticker", p.get("symbol", "")),
                "qty": float(p.get("qty", 0)),
                "current_price": float(p.get("current_price", 0)),
            }
            for p in positions
        ]
        last_scan = sched.get_last_scan()
        regime = last_scan.get("regime", "neutral")
        return risk.portfolio_risk_check(equity, pos_list, regime)
    except Exception as e:
        return {"error": str(e), "warnings": [str(e)]}


@app.get("/api/ai-status")
async def ai_status():
    last_scan = sched.get_last_scan()
    return {
        "enabled": is_enabled(),
        "approval_required": AI_APPROVAL_REQUIRED,
        "model": "claude-sonnet-4-6",
        "regime": last_scan.get("regime", "unknown"),
        "active_strategy": last_scan.get("active_strategy", "none"),
        "session_mode": last_scan.get("session_mode", "unknown"),
        "last_scan": last_scan.get("timestamp"),
    }


@app.post("/api/clear-trades")
async def clear_trades():
    """Eski test islem verilerini temizle."""
    return clear_old_trades()


@app.get("/api/journal")
async def get_journal(limit: int = 20):
    """Islem gunlugu — gecmis dersler."""
    return get_journal_entries(limit=limit)


@app.get("/api/performance")
async def get_performance():
    """Performans metrikleri — win rate, profit factor, drawdown."""
    return calculate_performance()


@app.post("/api/cancel-orders")
async def cancel_orders():
    """Tum bekleyen emirleri iptal et (pre-market cleanup)."""
    return broker.cancel_all_orders()


@app.get("/api/pending-orders")
async def pending_orders():
    """Bekleyen emirleri listele."""
    return broker.get_pending_orders()


@app.get("/api/account-status")
async def account_status():
    """Detayli hesap durumu — PDT, trading blocked, etc."""
    return broker.get_account_status()


@app.post("/api/approve-trade")
async def approve_trade(data: dict):
    """
    Dashboard'dan islem onaylama.
    Claude'un onerdigini kullanici onaylar, agent uygular.
    Safety rope: otonom mod oncesi son kontrol noktasi.
    """
    ticker = data.get("ticker", "")
    action = data.get("action", "")
    confidence = data.get("confidence", 0)
    entry_zone = data.get("entry_zone", "")

    if not ticker or not action:
        raise HTTPException(status_code=400, detail="ticker ve action gerekli")

    if action in ("hold", "watch"):
        return {"status": "skipped", "reason": "hold/watch icin islem yapilmaz"}

    loop = asyncio.get_event_loop()

    # Gercek fiyati al
    try:
        market_data = await loop.run_in_executor(None, get_market_data)
        ticker_data = market_data.get(ticker, {})
        current_price = ticker_data.get("price", 0)
        atr = ticker_data.get("atr14", current_price * 0.02)
    except Exception:
        raise HTTPException(status_code=400, detail="Fiyat verisi alinamadi")

    if current_price <= 0:
        raise HTTPException(status_code=400, detail=f"{ticker} icin fiyat bulunamadi")

    # Dinamik pozisyon boyutlandirma
    last_scan = sched.get_last_scan()
    regime = last_scan.get("regime", "neutral")

    stop_price = risk.atr_stop_loss(current_price, atr, "long" if action == "long" else "short")
    balance = await loop.run_in_executor(None, broker.get_balance)
    equity = balance  # Basitlestirilmis

    sizing = risk.dynamic_position_size(
        equity=equity,
        entry_price=current_price,
        stop_loss_price=stop_price,
        confidence=confidence,
        regime=regime,
    )

    qty = sizing.get("qty", 0)
    if qty <= 0:
        return {"status": "rejected", "reason": "Pozisyon boyutu 0 — guven skoru cok dusuk"}

    # Emri gonder
    try:
        result = await loop.run_in_executor(
            None,
            partial(broker.execute,
                action=action,
                ticker=ticker,
                qty=qty,
                price=current_price,
            )
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    if result.get("status") == "rejected":
        return result

    # Trade journal'a kaydet
    from trade_journal import log_journal_entry
    log_journal_entry(
        ticker=ticker,
        action=action,
        entry_price=current_price,
        qty=qty,
        ai_prediction=f"confidence={confidence}",
        ai_confidence=confidence,
        strategy_used=last_scan.get("active_strategy", ""),
        regime=regime,
    )

    # Dashboard'a bildir
    await manager.broadcast({
        "type": "new_trade",
        "ticker": ticker,
        "action": action,
        "qty": qty,
        "price": current_price,
        "status": result.get("status"),
        "source": "manual_approval",
    })

    return {
        "status": "ok",
        "ticker": ticker,
        "action": action,
        "qty": qty,
        "price": current_price,
        "sizing": sizing,
        "result": result,
    }


@app.get("/api/config")
async def get_config():
    """V3: Tüm konfigürasyonu döndür."""
    return cfg.get_all()


@app.get("/api/risk-metrics")
async def risk_metrics():
    """V3: Sharpe Ratio, Sortino, VaR hesapla."""
    journal = get_journal_entries(limit=100)
    returns = [
        e.get("pnl_pct", 0) for e in journal
        if isinstance(e, dict) and e.get("pnl_pct") is not None and e.get("pnl_pct") != 0
    ]
    return risk.calculate_risk_metrics(returns)


@app.get("/api/sector-exposure")
async def sector_exposure():
    """V3: Sektör bazlı portföy dağılımı."""
    try:
        acc = broker.get_account_status()
        positions = broker.get_all_positions()
        equity = float(acc.get("equity", 0))
        pos_list = [
            {
                "ticker": p.get("ticker", p.get("symbol", "")),
                "qty": float(p.get("qty", 0)),
                "current_price": float(p.get("current_price", 0)),
            }
            for p in positions
        ]
        return risk.check_sector_exposure(equity, pos_list)
    except Exception as e:
        return {"error": str(e), "sectors": {}, "warnings": []}


@app.get("/api/flash-crash-check")
async def flash_crash_check():
    """V3: Flash crash kontrol — anlık büyük düşüş tespiti."""
    try:
        positions = broker.get_all_positions()
        pos_list = [
            {
                "ticker": p.get("ticker", p.get("symbol", "")),
                "qty": float(p.get("qty", 0)),
                "current_price": float(p.get("current_price", 0)),
            }
            for p in positions
        ]
        loop = asyncio.get_event_loop()
        market_data = await loop.run_in_executor(None, get_market_data)
        return risk.check_flash_crash(pos_list, market_data)
    except Exception as e:
        return {"flash_crash_detected": False, "alerts": [], "error": str(e)}


@app.post("/api/emergency-liquidate")
async def emergency_liquidate():
    """V3: Acil durum — tüm pozisyonları kapat."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, broker.emergency_liquidate)
    await manager.broadcast({"type": "emergency_liquidate", "result": result})
    return result


@app.get("/api/multi-timeframe")
async def multi_timeframe():
    """V3: Multi-timeframe analiz — 1s, 4s, günlük confluence."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_multi_timeframe)


@app.get("/api/correlation")
async def correlation():
    """V3: Korelasyon matrisi — portföy diversifikasyon analizi."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_correlation_matrix)


# ─── V3.2: Backtesting ─────────────────────────────────────────

class BacktestRequest(BaseModel):
    ticker: str = "AKBNK.IS"
    days: int = Field(default=365, ge=90, le=1825)
    initial_capital: float = Field(default=100_000, ge=1000)
    risk_per_trade: float = Field(default=0.02, ge=0.005, le=0.10)
    atr_sl_multiplier: float = Field(default=1.5, ge=0.5, le=5.0)
    atr_tp_multiplier: float = Field(default=3.0, ge=1.0, le=10.0)
    min_momentum: int = Field(default=55, ge=30, le=80)


class PortfolioBacktestRequest(BaseModel):
    tickers: list[str] = []
    days: int = Field(default=365, ge=90, le=1825)
    initial_capital: float = Field(default=100_000, ge=1000)
    risk_per_trade: float = Field(default=0.02, ge=0.005, le=0.10)


@app.post("/api/backtest")
async def backtest(req: BacktestRequest):
    """V3.2: Tek hisse backtesti — sinyal stratejisini geçmiş veriye uygula."""
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: run_backtest(
        ticker=req.ticker,
        days=req.days,
        initial_capital=req.initial_capital,
        risk_per_trade=req.risk_per_trade,
        atr_sl_multiplier=req.atr_sl_multiplier,
        atr_tp_multiplier=req.atr_tp_multiplier,
        min_momentum=req.min_momentum,
    ))
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@app.post("/api/backtest/portfolio")
async def backtest_portfolio(req: PortfolioBacktestRequest):
    """V3.2: Portföy backtesti — tüm watchlist veya seçili hisseler."""
    tickers = req.tickers if req.tickers else WATCHLIST
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(None, lambda: run_portfolio_backtest(
        tickers=tickers,
        days=req.days,
        initial_capital=req.initial_capital,
        risk_per_trade=req.risk_per_trade,
    ))
    return result


# ─── V3.3: Regime + Sentiment + Anomaly ────────────────────────

@app.get("/api/regime")
async def regime():
    """V3.3: Kantitatif rejim tespiti — volatilite + trend + momentum + breadth."""
    loop = asyncio.get_event_loop()
    market_data = await loop.run_in_executor(None, get_market_data)
    if "error" in market_data:
        raise HTTPException(status_code=500, detail=market_data["error"])
    return await loop.run_in_executor(None, detect_regime, market_data)


@app.get("/api/news-sentiment")
async def news_sentiment():
    """V3.3: Tüm watchlist haber sentiment analizi."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_market_sentiment, WATCHLIST)


@app.get("/api/news-sentiment/{ticker}")
async def news_sentiment_ticker(ticker: str):
    """V3.3: Tek ticker haber sentiment analizi."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_ticker_sentiment, ticker.upper())


@app.get("/api/anomalies")
async def anomalies():
    """V3.3: Anormal piyasa davranışı tespiti."""
    loop = asyncio.get_event_loop()
    market_data = await loop.run_in_executor(None, get_market_data)
    if "error" in market_data:
        raise HTTPException(status_code=500, detail=market_data["error"])
    return await loop.run_in_executor(None, detect_anomalies, market_data)


# ─── V4.5: Gemini Council ──────────────────────────────────────

@app.get("/api/audit")
async def audit():
    """V4.5: Son Gemini audit sonuçları."""
    return get_last_audit()


# ─── V5: Monte Carlo Stress Test ─────────────────────────────────

class MonteCarloRequest(BaseModel):
    ticker: str = Field("XU100.IS", description="Returns source ticker")
    days_history: int = Field(365, ge=90, le=1825)
    initial_capital: float = Field(100000, ge=1000)
    num_simulations: int = Field(1000, ge=100, le=5000)
    num_days: int = Field(252, ge=30, le=756)
    ruin_threshold: float = Field(0.30, ge=0.1, le=0.8)

@app.post("/api/monte-carlo")
async def monte_carlo(req: MonteCarloRequest):
    """V5: Monte Carlo stress test — bootstrap ile portfoy simulasyonu."""
    loop = asyncio.get_event_loop()
    returns = await loop.run_in_executor(None, get_backtest_returns, req.ticker, req.days_history)
    if not returns or len(returns) < 10:
        raise HTTPException(status_code=400, detail="Yeterli veri yok. Farkli bir ticker veya daha uzun sure deneyin.")
    result = await loop.run_in_executor(
        None, run_monte_carlo, returns,
        req.initial_capital, req.num_simulations, req.num_days, 0.95, req.ruin_threshold,
    )
    return result

@app.post("/api/stress-test")
async def stress_test(req: MonteCarloRequest):
    """V5: Belirli stres senaryolari altinda portfoy testi."""
    loop = asyncio.get_event_loop()
    returns = await loop.run_in_executor(None, get_backtest_returns, req.ticker, req.days_history)
    if not returns or len(returns) < 10:
        raise HTTPException(status_code=400, detail="Yeterli veri yok.")
    result = await loop.run_in_executor(
        None, run_stress_scenarios, returns, req.initial_capital, req.num_days,
    )
    return result

# ─── V5: Strategy Optimizer ──────────────────────────────────────

class OptimizeRequest(BaseModel):
    ticker: str = Field("AKBNK.IS", description="Optimize edilecek ticker")
    days: int = Field(365, ge=90, le=1825)
    initial_capital: float = Field(100000, ge=1000)
    target_metric: str = Field("sharpe_ratio", description="sharpe_ratio | total_return_pct | max_drawdown_pct | profit_factor | win_rate")
    quick: bool = Field(False, description="Hizli optimizasyon (kucuk grid)")

@app.post("/api/optimize")
async def optimize(req: OptimizeRequest):
    """V5: Grid search ile strateji parametrelerini optimize et."""
    loop = asyncio.get_event_loop()
    if req.quick:
        result = await loop.run_in_executor(None, quick_optimize, req.ticker, req.days)
    else:
        result = await loop.run_in_executor(
            None, optimize_strategy, req.ticker, req.days, req.initial_capital, req.target_metric, None,
        )
    return result

# ─── V5: Advanced Trade Journal ──────────────────────────────────

class JournalEntry(BaseModel):
    ticker: str
    action: str
    side: str = "long"
    entry_price: float = 0
    exit_price: float = 0
    qty: float = 0
    setup_type: str = ""
    tags: list[str] = []
    notes: str = ""
    ai_confidence: int = 0
    regime: str = ""
    strategy: str = ""
    entry_reason: str = ""
    exit_reason: str = ""
    stop_loss: float | None = None
    take_profit: float | None = None

@app.post("/api/journal")
async def journal_add(entry: JournalEntry):
    """V5: Gelismis journal kaydı ekle."""
    return log_trade_v2(**entry.model_dump())

@app.get("/api/journal")
async def journal_list(
    limit: int = 50,
    ticker: str = None,
    tag: str = None,
    setup_type: str = None,
    side: str = None,
    winners_only: bool = False,
    losers_only: bool = False,
):
    """V5: Journal kayitlarini filtrele."""
    return get_journal_v2(
        limit=limit, ticker=ticker, tag=tag,
        setup_type=setup_type, side=side,
        winners_only=winners_only, losers_only=losers_only,
    )

@app.get("/api/journal/analytics")
async def journal_analytics():
    """V5: Kapsamli journal analitikleri."""
    return get_journal_analytics()

@app.get("/api/journal/export")
async def journal_export():
    """V5: Journal CSV export."""
    from fastapi.responses import PlainTextResponse
    csv_content = export_journal_csv()
    return PlainTextResponse(
        content=csv_content,
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=trade_journal.csv"},
    )


# ═════════════════════════════════════════════════════════════════
# MIDAS İŞLEMLERİM — Manuel emir takibi + AI karşılaştırması
# ═════════════════════════════════════════════════════════════════

class MidasLogRequest(BaseModel):
    ticker: str = Field(..., examples=["AKBNK"])
    action: str = Field("long", description="'long' alım | 'short' satım (BIST yasak)")
    order_type: str = Field("piyasa", description="piyasa | limit | stop | stop_limit | bracket")
    qty: float = Field(..., gt=0)
    entry_price: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    take_profit: float | None = None
    stop_loss: float | None = None
    time_in_force: str = Field("GUN", description="'GUN' günlük | 'IEK' iptale kadar")
    status: str = Field("open", description="'pending' dolmadı | 'open' dolu")
    ai_signal_source: str = ""
    ai_confidence: int = 0
    ai_reasoning: str = ""
    midas_order_id: str = ""
    is_margin: int = 0  # 0 = nakit, 1 = kredili
    notes: str = ""


class MidasCloseRequest(BaseModel):
    trade_id: int
    exit_price: float = Field(..., gt=0)
    exit_order_type: str = Field("piyasa", description="Çıkışta kullandığın emir tipi")
    notes: str = ""


class MidasFillRequest(BaseModel):
    trade_id: int
    fill_price: float = Field(..., gt=0)


class MidasCancelRequest(BaseModel):
    trade_id: int
    reason: str = ""


class MidasConfigRequest(BaseModel):
    starting_capital_try: float | None = None
    daily_goal_pct: float | None = None
    daily_max_loss_pct: float | None = None
    weekly_goal_pct: float | None = None
    monthly_goal_pct: float | None = None
    # Kişisel kriterler
    min_trade_value_try: float | None = None
    max_trade_value_try: float | None = None
    min_ai_confidence: int | None = None
    # Kredili işlem
    margin_enabled: int | None = None
    margin_min_confidence: int | None = None
    margin_max_position_pct: float | None = None
    margin_sectors: str | None = None


@app.get("/api/midas/order-types")
async def midas_order_types():
    """Midas'ın 5 emir tipi + süre seçenekleri (dropdown için)."""
    return midas.get_order_types_info()


# ─── AI Daily Briefing ─────────────────────────────────────────

class DailyPlanUpdateRequest(BaseModel):
    user_status: str  # 'approved' | 'adjusted' | 'skipped'
    daily_budget_try: float | None = None
    daily_profit_target_try: float | None = None
    max_trades: int | None = None
    notes: str = ""


@app.get("/api/daily-plan")
async def daily_plan_get(date: str = None):
    """Bugünün (veya belirtilen tarihin) AI briefing'ini döndür."""
    return midas.get_daily_plan(date)


@app.post("/api/daily-plan/generate")
async def daily_plan_generate():
    """
    AI'ya günün planını çıkar talimatı ver.
    09:30 TRT cron'la otomatik çalışır, ama manuel de tetiklenebilir.
    """
    loop = asyncio.get_event_loop()
    market_data = await loop.run_in_executor(None, get_market_data)
    if "error" in market_data:
        raise HTTPException(status_code=500, detail=market_data["error"])

    from claude_brain import generate_daily_plan
    portfolio = sched._get_portfolio(broker)
    recent = get_recent_trades(limit=10)

    plan = await loop.run_in_executor(
        None, generate_daily_plan, market_data, portfolio, recent
    )

    if "error" not in plan:
        midas.save_daily_plan(plan)

    return plan


@app.get("/api/daily-report")
async def daily_report(date: str = None):
    """Gün raporu — hedef vs gerçek + AI uyum + kullanım."""
    return midas.compute_daily_report(date)


@app.get("/api/calendar")
async def calendar(days: int = 30):
    """Son N gün günlük K/Z heatmap (takvim)."""
    return midas.get_calendar_heatmap(days=days)


@app.get("/api/cumulative")
async def cumulative(days: int = 90):
    """Son N gün kümülatif K/Z grafiği."""
    return midas.get_cumulative_series(days=days)


@app.get("/api/lifetime")
async def lifetime():
    """Tüm zamanlar — inception-to-date."""
    return midas.get_lifetime_stats()


@app.get("/api/live-prices")
async def live_prices():
    """Açık pozisyonların canlı fiyatları (30s cache, live watcher'dan)."""
    return sched.get_live_prices()


@app.post("/api/daily-plan/update")
async def daily_plan_update(req: DailyPlanUpdateRequest):
    """Kullanıcı AI planını onaylar / düzenler / atlar."""
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    now_tr = _dt.now(_tz.utc) + _td(hours=3)
    date = now_tr.strftime("%Y-%m-%d")
    return midas.update_plan_status(
        date=date,
        user_status=req.user_status,
        daily_budget_try=req.daily_budget_try,
        daily_profit_target_try=req.daily_profit_target_try,
        max_trades=req.max_trades,
        notes=req.notes,
    )


@app.post("/api/midas/log")
async def midas_log(req: MidasLogRequest):
    """Midas'ta verdiğin emri kaydet."""
    return midas.log_trade(**req.model_dump())


@app.post("/api/midas/fill")
async def midas_fill(req: MidasFillRequest):
    """Pending emir doldu — gerçekleşen fiyatı gir."""
    return midas.mark_as_open(req.trade_id, req.fill_price)


@app.post("/api/midas/cancel")
async def midas_cancel(req: MidasCancelRequest):
    """Pending emri iptal et."""
    return midas.cancel_trade(req.trade_id, req.reason)


@app.post("/api/midas/close")
async def midas_close(req: MidasCloseRequest):
    """Açık pozisyonu kapat (Midas'ta satış yaptın)."""
    return midas.close_trade(req.trade_id, req.exit_price, req.exit_order_type, req.notes)


@app.get("/api/midas/positions")
async def midas_positions(status: str = "open"):
    """Pozisyonları listele (open/pending/closed/all)."""
    return midas.get_positions(status=status)


@app.get("/api/midas/history")
async def midas_history(limit: int = 50, ticker: str = None, days: int = None):
    """Kapalı işlem geçmişi."""
    return midas.get_trade_history(limit=limit, ticker=ticker, days=days)


@app.get("/api/midas/daily")
async def midas_daily():
    """Bugünün özeti — K/Z, hedef, durum."""
    return midas.get_daily_stats()


@app.get("/api/midas/performance")
async def midas_performance(days: int = 30):
    """Son N gün performans — AI'a karşı gerçek."""
    return midas.get_performance_summary(days=days)


@app.get("/api/midas/config")
async def midas_config_get():
    """Midas config (sermaye, hedefler)."""
    return midas.get_config()


@app.post("/api/midas/config")
async def midas_config_set(req: MidasConfigRequest):
    """Midas config güncelle."""
    return midas.update_config(**req.model_dump(exclude_none=True))


@app.delete("/api/midas/trade/{trade_id}")
async def midas_delete(trade_id: int):
    """İşlem kaydını sil (yanlış girdiysen)."""
    return midas.delete_trade(trade_id)


@app.get("/api/health")
async def health():
    last_scan = sched.get_last_scan()
    return {
        "status": "ok",
        "version": "5.4",
        "ai_enabled": is_enabled(),
        "gemini_enabled": gemini_enabled(),
        "notify_enabled": notify_enabled(),
        "regime": last_scan.get("regime", "unknown"),
        "session_mode": last_scan.get("session_mode", "unknown"),
        "last_scan": last_scan.get("timestamp"),
    }


@app.post("/api/test-notification")
async def test_notification():
    """Test e-posta bildirimi gonder."""
    if not notify_enabled():
        return {"status": "error", "message": "NOTIFY_EMAIL veya SMTP_PASSWORD tanimli degil. Railway Variables'a ekleyin."}
    try:
        send_trade_notification(
            action="long",
            ticker="AKBNK.IS",
            qty=100,
            price=79.00,
            confidence=9,
            reasoning="Bu bir test bildirimidir. Meridian TR — BIST Trading Agent aktif ve calisiyor. Sistem dogru calisiyorsa bu maili alacaksiniz.",
            audit_verdict="APPROVE",
            stop_loss="77.00",
            take_profit="82.50",
            risk_pct=1.5,
        )
        return {"status": "ok", "message": "Test e-postasi gonderildi!"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


@app.get("/api/notify-debug")
async def notify_debug():
    """E-posta ayarlarini kontrol et (sifre gizlenir)."""
    smtp_email = os.getenv("SMTP_EMAIL") or os.getenv("NOTIFY_EMAIL") or ""
    notify_email = os.getenv("NOTIFY_EMAIL") or ""
    smtp_pass = os.getenv("SMTP_PASSWORD") or ""
    return {
        "notify_email": notify_email,
        "smtp_email": smtp_email,
        "smtp_password_length": len(smtp_pass),
        "smtp_password_preview": smtp_pass[:4] + "..." if len(smtp_pass) > 4 else "TOO_SHORT",
        "has_spaces": " " in smtp_pass,
        "notify_enabled": notify_enabled(),
    }
