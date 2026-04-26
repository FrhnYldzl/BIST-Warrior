"""
scheduler.py — Otonom Tarama Zamanlayıcısı (Meridian TR V1)

BIST saatine uyarlandı:
  - Zamanlayıcı TZ: Europe/Istanbul
  - Piyasa saati: Pzt-Cum 10:00-18:00 TRT
  - Pre-market YOK (BIST'te pre/after-hours seansı yok)

Modlar:
  - market_open  (Pzt-Cum 10:00-18:00 TRT): Aktif tarama, işlem tetikleyebilir
  - after_hours  (diğer zamanlar): Analiz + hazırlık modu, işlem yok

Her modda Claude beyni çalışır, sadece auto_execute davranışı değişir.
"""

import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from market_scanner import get_market_data, is_market_open
from claude_brain import run_brain, review_past_trades
from database import get_recent_trades
from trade_journal import init_journal_db
from notifier import send_trade_notification, send_morning_brief, send_high_conf_alert
from config import (
    OPERATION_MODE, HIGH_CONFIDENCE_ALERT,
    MORNING_BRIEF_HOUR, MORNING_BRIEF_MINUTE,
    DATA_DIR,
)

DB_PATH = Path(DATA_DIR) / "trades.db"
MARKET_TZ = "Europe/Istanbul"

# Son tarama sonucu (dashboard için)
_last_scan: dict = {
    "status": "Henuz tarama yapilmadi",
    "timestamp": None,
    "decisions": [],
    "regime": "unknown",
    "regime_reasoning": "",
    "active_strategy": "none",
    "market_summary": "",
    "portfolio_note": "",
    "watchlist_alerts": [],
    "market_data": {},
    "market_open": False,
    "session_mode": "initializing",
}

# Post-trade review sonucu
_last_review: dict = {
    "status": "Henuz review yapilmadi",
    "timestamp": None,
}

# High-conf alarm spam koruması (aynı ticker için saatte max 1 alarm)
_alert_sent: dict = {}

# Live watcher — her pozisyon için son uyarı zamanı (spam engeli)
_exit_alert_sent: dict = {}

# Canlı fiyat cache (30s poll, dashboard'dan da erişilir)
_live_prices: dict = {}

# Scheduler Istanbul TZ'de çalışır — tüm cron ifadeleri TRT
scheduler = BackgroundScheduler(timezone=MARKET_TZ)


def _now_tr_str() -> str:
    """TR saatinde okunabilir zaman damgası."""
    now_utc = datetime.now(timezone.utc)
    now_tr = now_utc + timedelta(hours=3)
    return now_tr.strftime("%Y-%m-%d %H:%M TRT")


# ─────────────────────────────────────────────────────────────────
# Ana tarama fonksiyonu
# ─────────────────────────────────────────────────────────────────

def run_scan(broker=None, auto_execute: bool = False):
    """
    Tek tarama döngüsü — PİYASA AÇIK VEYA KAPALI, HER ZAMAN ÇALIŞIR.

    Piyasa kapaliyken:
      - Tarihsel veri analizi yapar
      - Sonraki seans için plan hazırlar
      - Post-trade review yapar
      - İşlem tetiklemez (auto_execute kapalı)

    Piyasa açıkken:
      - Canlı veri analizi
      - İşlem önerileri (auto_execute=True ise uygulanır)
    """
    global _last_scan

    market_open = is_market_open()
    session_mode = "market_open" if market_open else "after_hours"

    print(f"[Scheduler] Tarama başlıyor — {_now_tr_str()} | Mod: {session_mode}")

    # 1. Piyasa verisi
    market_data = get_market_data()
    if "error" in market_data:
        _last_scan["status"] = f"Veri hatasi: {market_data['error']}"
        _last_scan["timestamp"] = datetime.now(timezone.utc).isoformat()
        _last_scan["session_mode"] = session_mode
        print(f"[Scheduler] Veri hatasi: {market_data['error']}")
        return

    # 2. Portföy durumu
    portfolio = _get_portfolio(broker)

    # 3. Claude kararı
    recent = get_recent_trades(limit=20)
    # BIST'te PDT yok — ama prompt'a bilgi gitmesi için 0 geçiyoruz
    portfolio["pdt_trades_left"] = 99  # PDT yok = sınırsız

    result = run_brain(
        market_data=market_data,
        portfolio=portfolio,
        recent_trades=recent,
        auto_execute=auto_execute and market_open,
    )

    # 4. Belleğe kaydet
    _last_scan = {
        "status": "ok",
        "timestamp": result.get("timestamp"),
        "decisions": result.get("decisions", []),
        "regime": result.get("regime", "unknown"),
        "regime_reasoning": result.get("regime_reasoning", ""),
        "active_strategy": result.get("active_strategy", "none"),
        "market_summary": result.get("market_summary", ""),
        "portfolio_note": result.get("portfolio_note", ""),
        "watchlist_alerts": result.get("watchlist_alerts", []),
        "market_data": market_data,
        "market_open": market_open,
        "session_mode": session_mode,
        "auto_execute": auto_execute and market_open,
    }

    # 5. DB'ye logla
    _log_scan(result)

    # 5b. Sinyal tarihçesine de logla (kalıcı: isabet takibi için)
    try:
        import signal_history as _sighist
        _sighist.log_scan(result, market_data=market_data)
    except Exception as e:
        print(f"[SignalHistory] log hatası: {e}")

    # 6. Gemini Audit (Council — iki AI onaylarsa işlem yapılır)
    audit_results = []
    gemini_status = "ok"
    try:
        from gemini_auditor import audit_decisions, is_enabled as gemini_enabled
        if gemini_enabled() and result.get("decisions"):
            audit_results = audit_decisions(
                decisions=result.get("decisions", []),
                market_data=market_data,
                portfolio=portfolio,
                regime=result.get("regime", "unknown"),
            )
    except Exception as e:
        gemini_status = "unavailable"
        print(f"[Gemini Audit] Kullanılamıyor (fallback: Claude-only): {e}")
        for d in result.get("decisions", []):
            audit_results.append({
                "ticker": d.get("ticker", ""),
                "audit_verdict": "APPROVE",
                "reasoning": "Gemini unavailable — auto-approved by Claude-only fallback",
                "risk_flag": "gemini_offline",
            })

    _last_scan["audit_results"] = audit_results
    _last_scan["gemini_status"] = gemini_status

    # 7. Otomatik işlem (sadece auto modda + piyasa açıkken)
    if auto_execute and market_open and broker and OPERATION_MODE != "analyst":
        _execute_decisions(result.get("decisions", []), broker, portfolio, market_data, audit_results)

    # 7b. ANALYST MODE — yüksek güvenli sinyaller için anlık alarm
    if OPERATION_MODE == "analyst" and HIGH_CONFIDENCE_ALERT and market_open:
        for d in result.get("decisions", []):
            if d.get("action") == "long" and d.get("confidence", 0) >= 9:
                ticker = d.get("ticker", "")
                price = market_data.get(ticker, {}).get("price", 0)
                # Son 30 dakikada aynı ticker için alarm gönderildi mi? (spam koruması)
                key = f"alert_{ticker}_{datetime.now(timezone.utc).strftime('%Y%m%d%H')}"
                if not _alert_sent.get(key):
                    try:
                        send_high_conf_alert(d, market_price=price)
                        _alert_sent[key] = True
                    except Exception as e:
                        print(f"[Scheduler] Alarm hatası {ticker}: {e}")

    actionable = [d for d in result.get("decisions", [])
                  if d.get("action") not in ("hold", "watch")]
    mode_tag = "📱 analyst" if OPERATION_MODE == "analyst" else "🤖 auto"
    print(f"[Scheduler] Tarama tamam [{mode_tag}] | Rejim: {result.get('regime','?')} | "
          f"Strateji: {result.get('active_strategy','?')} | "
          f"{len(actionable)} aksiyon karari")


def run_review(broker=None):
    """Post-trade review — öğrenme döngüsü."""
    global _last_review

    portfolio = _get_portfolio(broker)
    recent = get_recent_trades(limit=20)

    if not recent:
        _last_review = {"status": "Islem gecmisi yok", "timestamp": datetime.now(timezone.utc).isoformat()}
        return

    result = review_past_trades(recent, portfolio)
    result["timestamp"] = datetime.now(timezone.utc).isoformat()
    _last_review = result

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    grade TEXT,
                    lessons TEXT,
                    adjustments TEXT
                )
            """)
            conn.execute(
                "INSERT INTO reviews (timestamp, grade, lessons, adjustments) VALUES (?,?,?,?)",
                (
                    result.get("timestamp"),
                    result.get("overall_grade", "?"),
                    json.dumps(result.get("lessons", [])),
                    json.dumps(result.get("strategy_adjustments", [])),
                )
            )
            conn.commit()
    except Exception:
        pass

    print(f"[Scheduler] Post-trade review tamam | Not: {result.get('overall_grade', '?')}")


def get_last_scan() -> dict:
    return _last_scan


def get_last_review() -> dict:
    return _last_review


# ─────────────────────────────────────────────────────────────────
# Scheduler başlat / durdur
# ─────────────────────────────────────────────────────────────────

def start(broker=None, auto_execute: bool = False, interval_minutes: int = 10):
    """Arka planda zamanlayıcıyı başlat (TR saat dilimi — Europe/Istanbul)."""
    if scheduler.running:
        return

    init_journal_db()
    try:
        import signal_history as _sighist
        _sighist.init_db()
    except Exception as e:
        print(f"[SignalHistory] init hatası: {e}")

    # Pre-market cleanup: bekleyen stale emirleri iptal et
    if broker:
        try:
            cleanup = broker.cancel_all_orders()
            print(f"[Startup Cleanup] {cleanup.get('message', '?')}")
        except Exception as e:
            print(f"[Startup Cleanup] hata: {e}")

    # Ana tarama: her N dakika (piyasa açık/kapalı farketmez)
    scheduler.add_job(
        func=lambda: run_scan(broker=broker, auto_execute=auto_execute),
        trigger=IntervalTrigger(minutes=interval_minutes),
        id="market_scan",
        replace_existing=True,
    )

    # Post-trade review: Pzt-Cum 19:00 TRT (BIST kapanışından ~1 saat sonra)
    scheduler.add_job(
        func=lambda: run_review(broker=broker),
        trigger=CronTrigger(day_of_week="mon-fri", hour=19, minute=0, timezone=MARKET_TZ),
        id="post_trade_review",
        replace_existing=True,
    )

    # 🌅 Sabah özeti: Pzt-Cum <HOUR>:<MINUTE> TRT (BIST açılışından önce)
    scheduler.add_job(
        func=lambda: _send_morning_brief(broker=broker),
        trigger=CronTrigger(day_of_week="mon-fri",
                           hour=MORNING_BRIEF_HOUR,
                           minute=MORNING_BRIEF_MINUTE,
                           timezone=MARKET_TZ),
        id="morning_brief",
        replace_existing=True,
    )

    # 🧠 AI Daily Briefing: Pzt-Cum 09:30 TRT (BIST açılışına yakın)
    scheduler.add_job(
        func=lambda: _generate_daily_briefing(broker=broker),
        trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=MARKET_TZ),
        id="daily_ai_briefing",
        replace_existing=True,
    )

    # 👁 Live Position Watcher — her 30s açık pozisyonları izle (piyasa saatlerinde)
    scheduler.add_job(
        func=watch_open_positions,
        trigger=IntervalTrigger(seconds=30),
        id="live_position_watcher",
        replace_existing=True,
    )

    # ⏱ Signal Hit Tracker — her 90s bekleyen sinyallerin entry/TP/SL durumunu güncelle
    scheduler.add_job(
        func=_update_signal_hits,
        trigger=IntervalTrigger(seconds=90),
        id="signal_hit_tracker",
        replace_existing=True,
    )

    # 🌆 Signal EOD Finalizer — Pzt-Cum 18:05 TRT (seans kapanışından 5 dk sonra)
    scheduler.add_job(
        func=_finalize_signals_eod,
        trigger=CronTrigger(day_of_week="mon-fri", hour=18, minute=5, timezone=MARKET_TZ),
        id="signal_eod_finalizer",
        replace_existing=True,
    )

    # Pre-open cleanup: Pzt-Cum 09:30 TRT (açılıştan 30 dk önce)
    scheduler.add_job(
        func=lambda: _pre_open_cleanup(broker),
        trigger=CronTrigger(day_of_week="mon-fri", hour=9, minute=30, timezone=MARKET_TZ),
        id="pre_open_cleanup",
        replace_existing=True,
    )

    # İlk taramayı 5 sn sonra başlat (non-blocking)
    scheduler.add_job(
        func=lambda: run_scan(broker=broker, auto_execute=auto_execute),
        trigger="date",
        run_date=datetime.now(timezone.utc) + timedelta(seconds=5),
        id="first_scan",
        replace_existing=True,
    )

    scheduler.start()
    mode_str = "📱 ANALYST (Midas)" if OPERATION_MODE == "analyst" else "🤖 AUTO-EXEC"
    print(f"[Scheduler] Meridian TR [{mode_str}] başlatıldı (TZ: {MARKET_TZ})")
    print(f"[Scheduler]   • Her {interval_minutes}dk tarama")
    print(f"[Scheduler]   • {MORNING_BRIEF_HOUR:02d}:{MORNING_BRIEF_MINUTE:02d} TRT sabah özeti (Pzt-Cum)")
    print(f"[Scheduler]   • 09:30 TRT emir temizliği, 19:00 TRT haftalık review")
    if OPERATION_MODE == "analyst":
        print(f"[Scheduler]   • Analyst mode: güven ≥9 için anlık e-posta alarmı")
    print(f"[Scheduler]   • İlk tarama 5sn sonra başlıyor")


def stop():
    if scheduler.running:
        scheduler.shutdown(wait=False)
        print("[Scheduler] Durduruldu.")


# ──────────────────────────────────────────────────────────────────
# Signal hit tracker — entry/TP/SL gerçekleşti mi?
# ──────────────────────────────────────────────────────────────────

def _update_signal_hits():
    """Her 90s çalışır: bekleyen/girilmiş sinyalleri canlı fiyatla karşılaştır."""
    try:
        import signal_history as _sighist
        from market_scanner import is_market_open
        if not is_market_open():
            return

        # Bugünün aktif sinyallerini topla, onlara ait tickers'ı al
        active = _sighist.get_active_signals()
        if not active:
            return

        tickers = list({s["ticker"] for s in active})
        if not tickers:
            return

        # yfinance ile canlı fiyatlar
        import yfinance as yf
        import pandas as pd
        live_prices: dict = {}

        try:
            df = yf.download(
                tickers=" ".join(tickers),
                period="1d", interval="5m",
                group_by="ticker", threads=True,
                progress=False, auto_adjust=True,
            )
            if isinstance(df.columns, pd.MultiIndex):
                for t in tickers:
                    if t in df.columns.get_level_values(0):
                        sub = df[t].dropna(how="all")
                        if len(sub):
                            live_prices[t] = float(sub["Close"].iloc[-1])
            else:
                # Tek ticker
                sub = df.dropna(how="all")
                if len(sub) and tickers:
                    live_prices[tickers[0]] = float(sub["Close"].iloc[-1])
        except Exception as e:
            print(f"[SignalHits] yfinance hatası: {e}")
            return

        # Durumu güncelle + değişikliklere göre bildirim tetikle
        result = _sighist.update_hit_status(live_prices)

        # Dashboard'a WebSocket push (varsa)
        if (result.get("newly_approaching") or result.get("newly_entered")
            or result.get("tp_hits") or result.get("sl_hits")):
            try:
                from main import manager as _ws_manager  # lazy import to avoid cycle
                import asyncio
                payload = {
                    "type": "signal_update",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    **result,
                }
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.ensure_future(_ws_manager.broadcast(payload))
            except Exception:
                pass

        if result["updated"]:
            print(f"[SignalHits] {result['updated']} güncellendi | entered={len(result['newly_entered'])} TP={len(result['tp_hits'])} SL={len(result['sl_hits'])}")
    except Exception as e:
        print(f"[SignalHits] beklenmeyen hata: {e}")


def _finalize_signals_eod():
    """18:05 TRT: kapanmamış sinyalleri 'expired' olarak işaretle."""
    try:
        import signal_history as _sighist
        n = _sighist.mark_expired_at_eod()
        print(f"[SignalEOD] {n} sinyal 'expired' olarak işaretlendi")
    except Exception as e:
        print(f"[SignalEOD] hata: {e}")


# ─────────────────────────────────────────────────────────────────
# Yardımcılar
# ─────────────────────────────────────────────────────────────────

def _get_portfolio(broker) -> dict:
    """
    Broker'dan portföy bilgisi al (BIST broker API — get_account_status + get_all_positions).

    BIST broker (equity.py / ibkr.py) ortak arayüzü destekler;
    bu fonksiyon her iki durumda da çalışır.
    """
    if broker is None:
        return {"cash": 0, "equity": 0, "positions": []}
    try:
        acc = broker.get_account_status()
        if "error" in acc:
            return {"cash": 0, "equity": 0, "positions": [], "error": acc["error"]}

        positions = broker.get_all_positions()
        return {
            "cash":         float(acc.get("cash", 0)),
            "equity":       float(acc.get("equity", 0)),
            "buying_power": float(acc.get("buying_power", acc.get("cash", 0))),
            "portfolio_value": float(acc.get("portfolio_value", acc.get("equity", 0))),
            "total_return_pct": float(acc.get("total_return_pct", 0)),
            "currency":     acc.get("currency", "TRY"),
            "positions":    positions,
        }
    except Exception as e:
        return {"cash": 0, "equity": 0, "positions": [], "error": str(e)}


def _execute_decisions(decisions: list, broker, portfolio: dict, market_data: dict, audit_results: list = None):
    """
    Aksiyon kararlarını broker'a ilet — Gemini Council onayı ile.
    BIST'te short yasak — Claude'un short önermesi halinde burada da bloklanır.
    """
    from risk_manager import RiskManager
    from config import SHORT_ENABLED, CURRENCY_SYMBOL
    risk = RiskManager(max_risk_pct=0.02)
    equity = portfolio.get("equity", 0)

    audit_map = {a.get("ticker", ""): a for a in (audit_results or [])}

    for d in decisions:
        action     = d.get("action", "hold")
        ticker     = d.get("ticker", "")
        confidence = d.get("confidence", 0)

        if action in ("hold", "watch", "reduce") or not ticker:
            continue

        # BIST'te short yasağı — konfigürasyonla zorla
        if action == "short" and not SHORT_ENABLED:
            print(f"[Auto] {ticker} atlandi — BIST'te short yasak (SHORT_ENABLED=False)")
            continue

        if confidence < 6:
            print(f"[Auto] {ticker} atlandi — güven skoru düşük ({confidence}/10)")
            continue

        # Gemini Council
        audit = audit_map.get(ticker)
        if audit:
            verdict = audit.get("audit_verdict", "APPROVE")
            if verdict == "REJECT":
                print(f"[Council] {ticker} REDDEDİLDİ — Gemini: {audit.get('reasoning', '?')}")
                continue
            elif verdict == "MODIFY":
                mods = audit.get("modified_params", {})
                if "position_size_pct" in mods:
                    d["position_size_pct"] = mods["position_size_pct"]
                print(f"[Council] {ticker} MODİFİYE — Gemini: {audit.get('reasoning', '?')}")
            else:
                print(f"[Council] {ticker} ONAYLANDI — Gemini + Claude hemfikir")

        try:
            ticker_data = market_data.get(ticker, {})
            price = ticker_data.get("price", 0)
            atr   = ticker_data.get("atr14", price * 0.02)

            if price <= 0:
                continue

            direction = "long" if action == "long" else "short"
            stop_price = risk.atr_stop_loss(price, atr, direction, multiplier=1.5)

            regime = d.get("strategy", "neutral")
            sizing = risk.dynamic_position_size(
                equity=equity,
                entry_price=price,
                stop_loss_price=stop_price,
                confidence=confidence,
                regime=regime,
            )

            qty = sizing.get("qty", 0)
            if qty <= 0:
                continue

            broker.execute(action, ticker, qty, price)
            print(f"[Auto] {action.upper()} {ticker} x{qty} @ {CURRENCY_SYMBOL}{price:.2f} "
                  f"(confidence={confidence}, risk={sizing.get('risk_pct',0)}%)")

            try:
                audit = audit_map.get(ticker, {})
                send_trade_notification(
                    action=action,
                    ticker=ticker,
                    qty=qty,
                    price=price,
                    confidence=confidence,
                    reasoning=d.get("reasoning", ""),
                    audit_verdict=audit.get("audit_verdict", "APPROVE"),
                    stop_loss=d.get("stop_loss", ""),
                    take_profit=d.get("take_profit", ""),
                    risk_pct=sizing.get("risk_pct", 0),
                )
            except Exception as e:
                print(f"[Notifier] Bildirim hatasi: {e}")
        except Exception as e:
            print(f"[Auto] HATA {ticker}: {e}")


def watch_open_positions():
    """
    Açık Midas pozisyonlarını canlı izle, TP/SL yakınlık uyarıları.
    Her 30 saniyede çalışır. Sadece piyasa açıkken anlamlı.
    """
    try:
        from market_scanner import is_market_open
        import midas_journal as _midas
        if not is_market_open():
            return

        open_positions = _midas.get_positions(status="open")
        if not open_positions:
            return

        import yfinance as yf
        tickers = list({p["ticker"] for p in open_positions})
        if not tickers:
            return

        # Batch download
        try:
            df = yf.download(
                tickers=" ".join(tickers),
                period="1d", interval="5m",
                group_by="ticker", threads=True,
                progress=False, auto_adjust=True,
            )
        except Exception as e:
            print(f"[LiveWatcher] yfinance hatası: {e}")
            return

        import pandas as pd
        now_utc = datetime.now(timezone.utc)
        now_tr = now_utc + timedelta(hours=3)

        for pos in open_positions:
            ticker = pos["ticker"]
            try:
                if isinstance(df.columns, pd.MultiIndex):
                    if ticker not in df.columns.get_level_values(0):
                        continue
                    t_df = df[ticker].dropna(how="all")
                else:
                    t_df = df.dropna(how="all")

                if len(t_df) < 1:
                    continue

                current = float(t_df["Close"].iloc[-1])
                _live_prices[ticker] = {
                    "price": current,
                    "updated_at": now_utc.isoformat(),
                }

                entry = pos.get("entry_price", 0) or 0
                sl = pos.get("stop_loss", 0) or 0
                tp = pos.get("take_profit", 0) or 0
                pos_id = pos.get("id")

                # Anlık K/Z
                pnl_pct = ((current - entry) / entry * 100) if entry > 0 else 0

                alert_key_base = f"{pos_id}_{ticker}"
                hour_key = now_utc.strftime("%Y%m%d%H")

                # TP yakın (%2 içinde ama geçmedi)
                if tp > 0 and current >= tp * 0.98 and current < tp * 1.01:
                    k = f"{alert_key_base}_tp_{hour_key}"
                    if not _exit_alert_sent.get(k):
                        _exit_alert_sent[k] = True
                        print(f"[LiveWatcher] 🎯 {ticker} HEDEF YAKIN: ₺{current:.2f} / TP ₺{tp:.2f} (+{pnl_pct:.1f}%)")
                        _send_exit_alert(pos, current, "target_near", pnl_pct)

                # TP geçti (çıkış zamanı)
                elif tp > 0 and current >= tp:
                    k = f"{alert_key_base}_tp_hit_{hour_key}"
                    if not _exit_alert_sent.get(k):
                        _exit_alert_sent[k] = True
                        print(f"[LiveWatcher] ✅ {ticker} HEDEFE ULAŞTI: ₺{current:.2f} / TP ₺{tp:.2f} — ÇIKIŞ ZAMANI!")
                        _send_exit_alert(pos, current, "target_hit", pnl_pct)

                # SL yakın (%2 içinde ama geçmedi)
                elif sl > 0 and current <= sl * 1.02 and current > sl * 0.99:
                    k = f"{alert_key_base}_sl_{hour_key}"
                    if not _exit_alert_sent.get(k):
                        _exit_alert_sent[k] = True
                        print(f"[LiveWatcher] ⚠️ {ticker} STOP YAKIN: ₺{current:.2f} / SL ₺{sl:.2f} ({pnl_pct:.1f}%)")
                        _send_exit_alert(pos, current, "stop_near", pnl_pct)

                # SL kırıldı
                elif sl > 0 and current <= sl:
                    k = f"{alert_key_base}_sl_hit_{hour_key}"
                    if not _exit_alert_sent.get(k):
                        _exit_alert_sent[k] = True
                        print(f"[LiveWatcher] 🚨 {ticker} STOP KIRILDI: ₺{current:.2f} / SL ₺{sl:.2f} — ACİL ÇIKIŞ!")
                        _send_exit_alert(pos, current, "stop_hit", pnl_pct)

                # Kredili işlem + 17:30 geçti uyarı
                if pos.get("is_margin") and now_tr.hour == 17 and now_tr.minute >= 30:
                    k = f"{alert_key_base}_credit_{now_tr.strftime('%Y%m%d')}"
                    if not _exit_alert_sent.get(k):
                        _exit_alert_sent[k] = True
                        print(f"[LiveWatcher] 🔴 KREDİLİ {ticker}: gün-içi kapat! ({now_tr.strftime('%H:%M')} TRT)")
                        _send_exit_alert(pos, current, "credit_eod", pnl_pct)

                # Gün sonu genel uyarı (17:55)
                if now_tr.hour == 17 and now_tr.minute >= 55:
                    k = f"{alert_key_base}_eod_{now_tr.strftime('%Y%m%d')}"
                    if not _exit_alert_sent.get(k):
                        _exit_alert_sent[k] = True
                        print(f"[LiveWatcher] 🌆 {ticker}: GÜN SONU YAKLAŞIYOR ({now_tr.strftime('%H:%M')} TRT)")
                        _send_exit_alert(pos, current, "eod_warning", pnl_pct)

            except Exception as e:
                print(f"[LiveWatcher] {ticker} hata: {e}")

        # Spam cache temizle (gün sonunda)
        if now_tr.hour == 18 and now_tr.minute >= 30:
            _exit_alert_sent.clear()

    except Exception as e:
        print(f"[LiveWatcher] Global hata: {e}")


def _send_exit_alert(pos: dict, current_price: float, reason: str, pnl_pct: float):
    """Çıkış uyarısı — e-posta + WebSocket broadcast."""
    try:
        from notifier import is_enabled as notify_enabled
        # Dashboard için state'e yaz (frontend WS ile alır)
        # E-posta gönderimi: sadece önemli senaryolarda
        if reason in ("target_hit", "stop_hit", "credit_eod", "eod_warning"):
            try:
                from notifier import send_high_conf_alert
                ticker = pos["ticker"].replace(".IS", "")
                pseudo_decision = {
                    "ticker": ticker,
                    "action": "CLOSE",
                    "confidence": 10,
                    "strategy": reason,
                    "reasoning": f"Pozisyon #{pos.get('id')} · Giriş ₺{pos.get('entry_price',0):.2f} · Şu an ₺{current_price:.2f} · K/Z {pnl_pct:+.2f}%",
                    "entry_zone": f"{current_price:.2f}",
                    "stop_loss": str(pos.get("stop_loss") or ""),
                    "take_profit": str(pos.get("take_profit") or ""),
                }
                send_high_conf_alert(pseudo_decision, market_price=current_price)
            except Exception as e:
                print(f"[ExitAlert] Mail hatası: {e}")
    except Exception as e:
        print(f"[ExitAlert] {e}")


def get_live_prices() -> dict:
    """Dashboard için canlı fiyat cache'i."""
    return _live_prices


def _generate_daily_briefing(broker):
    """
    09:30 TRT — AI günün planını hazırlar (bütçe, hedef, max işlem, kredili mi?).
    Dashboard'da kullanıcı onayı bekler.
    """
    print(f"[Scheduler] 🧠 AI Daily Briefing başlıyor ({_now_tr_str()})")
    try:
        from market_scanner import get_market_data
        from claude_brain import generate_daily_plan
        from database import get_recent_trades
        import midas_journal as _midas

        market_data = get_market_data()
        if "error" in market_data:
            print(f"[Scheduler] Briefing atlandı — {market_data['error']}")
            return

        portfolio = _get_portfolio(broker)
        recent = get_recent_trades(limit=10)
        plan = generate_daily_plan(market_data, portfolio, recent)

        if "error" not in plan:
            _midas.save_daily_plan(plan)
            quality = plan.get("day_quality", "unknown")
            budget = plan.get("daily_budget_try", 0)
            target = plan.get("daily_profit_target_try", 0)
            print(f"[Scheduler] ✓ Briefing hazır: {quality.upper()} · Bütçe ₺{budget:,.0f} · Hedef ₺{target:,.0f}")
        else:
            print(f"[Scheduler] Briefing hatası: {plan.get('error')}")
    except Exception as e:
        print(f"[Scheduler] Briefing exception: {e}")


def _send_morning_brief(broker):
    """
    Sabah 09:45 TRT — günün planını taze tarama ile üret ve e-posta gönder.
    Analyst modda kullanıcının Midas'a elle girmesi için hazır liste.
    """
    print(f"[Scheduler] 🌅 Sabah özeti başlıyor ({_now_tr_str()})")
    try:
        # Taze bir tarama yap
        run_scan(broker=broker, auto_execute=False)
        scan = _last_scan
        if scan.get("status") == "ok":
            send_morning_brief(scan)
            # Alarm spam cache'ini sıfırla (yeni gün)
            _alert_sent.clear()
        else:
            print(f"[Scheduler] Sabah özeti atlandı — tarama hatası")
    except Exception as e:
        print(f"[Scheduler] Sabah özeti hatası: {e}")


def _pre_open_cleanup(broker):
    """
    Açılıştan önce temizlik: bekleyen stale emirleri iptal et.
    Her iş günü 09:30 TRT (BIST açılışından 30 dk önce) çalışır.
    """
    if broker is None:
        return
    try:
        pending = broker.get_pending_orders()
        if pending and len(pending) > 0 and not any("error" in p for p in pending):
            result = broker.cancel_all_orders()
            print(f"[Pre-Open Cleanup] {_now_tr_str()} — {result.get('message', '?')}")
        else:
            print(f"[Pre-Open Cleanup] {_now_tr_str()} — Bekleyen emir yok, temiz.")
    except Exception as e:
        print(f"[Pre-Open Cleanup] Hata: {e}")


def _log_scan(result: dict):
    """Tarama sonucunu DB'ye kaydet."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS scans (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    regime TEXT,
                    active_strategy TEXT,
                    decisions TEXT,
                    market_summary TEXT,
                    portfolio_note TEXT
                )
            """)
            conn.execute(
                "INSERT INTO scans (timestamp, regime, active_strategy, decisions, market_summary, portfolio_note) VALUES (?,?,?,?,?,?)",
                (
                    result.get("timestamp"),
                    result.get("regime", ""),
                    result.get("active_strategy", ""),
                    json.dumps(result.get("decisions", [])),
                    result.get("market_summary", ""),
                    result.get("portfolio_note", ""),
                )
            )
            conn.commit()
    except Exception:
        pass
