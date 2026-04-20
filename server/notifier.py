"""
notifier.py — Meridian TR Bildirim Sistemi (V1)

İşlem gerçekleştiğinde e-posta bildirimi gönderir.
Resend API (HTTP) + Gmail SMTP fallback. BIST'e uyarlandı (₺, TRT saat).
"""

import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv, dotenv_values

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)
_env_vals = dotenv_values(_env_path)

def _get(key): return os.getenv(key) or _env_vals.get(key, "")

try:
    from config import CURRENCY_SYMBOL
except Exception:
    CURRENCY_SYMBOL = "₺"


def is_enabled() -> bool:
    return bool(_get("SMTP_PASSWORD") and _get("NOTIFY_EMAIL"))


def send_trade_notification(
    action: str,
    ticker: str,
    qty: int,
    price: float,
    confidence: int,
    reasoning: str = "",
    audit_verdict: str = "APPROVE",
    stop_loss: str = "",
    take_profit: str = "",
    risk_pct: float = 0,
):
    """Islem gerceklestiginde e-posta gonder."""
    if not is_enabled():
        print("[Notifier] SMTP_PASSWORD veya NOTIFY_EMAIL tanimli degil, bildirim atladiyor")
        return

    to_email = _get("NOTIFY_EMAIL")
    smtp_email = _get("SMTP_EMAIL") or to_email
    smtp_password = _get("SMTP_PASSWORD")

    # BIST TZ (TRT = UTC+3)
    now = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d %H:%M TRT")
    sym = CURRENCY_SYMBOL

    # Emoji ve renk
    action_upper = action.upper()
    if action_upper in ("LONG", "BUY"):
        icon = "ALIM"
        color = "#22c55e"
    elif action_upper in ("SHORT", "SELL"):
        icon = "SATIM"
        color = "#ef4444"
    elif "CLOSE" in action_upper:
        icon = "KAPAT"
        color = "#f59e0b"
    else:
        icon = action_upper
        color = "#3b82f6"

    subject = f"[BIST Warrior] {icon} {ticker} x{qty} @ {sym}{price:.2f}"

    html = f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 500px; margin: 0 auto; background: #0a0e17; color: #e2e8f0; border-radius: 12px; overflow: hidden; border: 1px solid #1e293b;">
        <div style="background: {color}; padding: 16px 24px;">
            <h2 style="margin: 0; color: white; font-size: 18px;">{icon} {ticker}</h2>
            <p style="margin: 4px 0 0; color: rgba(255,255,255,0.85); font-size: 13px;">{now}</p>
        </div>
        <div style="padding: 24px;">
            <table style="width: 100%; border-collapse: collapse; font-size: 14px;">
                <tr>
                    <td style="padding: 8px 0; color: #94a3b8;">Aksiyon</td>
                    <td style="padding: 8px 0; text-align: right; font-weight: 600; color: {color};">{action_upper}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #94a3b8;">Adet</td>
                    <td style="padding: 8px 0; text-align: right; color: #e2e8f0;">{qty} hisse</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #94a3b8;">Fiyat</td>
                    <td style="padding: 8px 0; text-align: right; color: #e2e8f0;">{sym}{price:.2f}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #94a3b8;">Toplam Tutar</td>
                    <td style="padding: 8px 0; text-align: right; color: #e2e8f0;">{sym}{qty * price:,.2f}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #94a3b8;">Güven Skoru</td>
                    <td style="padding: 8px 0; text-align: right; color: #e2e8f0;">{confidence}/10</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #94a3b8;">Stop-Loss</td>
                    <td style="padding: 8px 0; text-align: right; color: #ef4444;">{stop_loss or '—'}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #94a3b8;">Kâr Al</td>
                    <td style="padding: 8px 0; text-align: right; color: #22c55e;">{take_profit or '—'}</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #94a3b8;">Risk %</td>
                    <td style="padding: 8px 0; text-align: right; color: #e2e8f0;">{risk_pct:.1f}%</td>
                </tr>
                <tr>
                    <td style="padding: 8px 0; color: #94a3b8;">Council</td>
                    <td style="padding: 8px 0; text-align: right; color: {'#22c55e' if audit_verdict == 'APPROVE' else '#f59e0b'};">{audit_verdict}</td>
                </tr>
            </table>

            <div style="margin-top: 16px; padding: 12px; background: #111827; border-radius: 8px; border-left: 3px solid {color};">
                <p style="margin: 0; font-size: 12px; color: #94a3b8;">AI Gerekçe</p>
                <p style="margin: 6px 0 0; font-size: 13px; color: #cbd5e1; line-height: 1.5;">{reasoning[:300]}</p>
            </div>

            <p style="margin-top: 20px; font-size: 11px; color: #475569; text-align: center;">
                Meridian TR — BIST Otonom Trading Agent
            </p>
        </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_email
    msg["To"] = to_email

    # Plain text fallback
    plain = f"{icon} {ticker}\n{action_upper} {qty} hisse @ {sym}{price:.2f}\nGüven: {confidence}/10\nCouncil: {audit_verdict}\n\n{reasoning[:200]}"
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    _send_email(smtp_email, smtp_password, to_email, msg)
    print(f"[Notifier] E-posta gonderildi: {subject}")


def _send_email(smtp_email, smtp_password, to_email, msg):
    """E-posta gonder. Tum yontemleri dener: Resend API, Gmail SSL, Gmail TLS."""
    import urllib.request
    import urllib.error

    errors = []

    # Yontem 1: Resend API (HTTP — Railway'de SMTP bloklu oldugu icin)
    resend_key = os.getenv("RESEND_API_KEY") or _get("RESEND_API_KEY")
    if resend_key:
        try:
            payload = json.dumps({
                "from": "Meridian TR <onboarding@resend.dev>",
                "to": [to_email],
                "subject": msg["Subject"],
                "html": [p.get_payload(decode=True).decode() for p in msg.get_payload() if p.get_content_type() == "text/html"][0],
            }).encode()
            req = urllib.request.Request(
                "https://api.resend.com/emails",
                data=payload,
                headers={"Authorization": f"Bearer {resend_key}", "Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_text = resp.read().decode()
                print(f"[Notifier] Resend API ile gonderildi: {resp_text}")
            return
        except Exception as e:
            err_msg = str(e)
            if hasattr(e, 'read'):
                try: err_msg = e.read().decode()
                except: pass
            errors.append(f"Resend: {err_msg}")
            print(f"[Notifier] Resend basarisiz: {err_msg}")

    # Yontem 2: Gmail SSL (port 465)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, to_email, msg.as_string())
        print("[Notifier] Gmail SSL (465) ile gonderildi")
        return
    except Exception as e:
        errors.append(f"SSL-465: {e}")
        print(f"[Notifier] SSL (465) basarisiz: {e}")

    # Yontem 3: Gmail TLS (port 587)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, to_email, msg.as_string())
        print("[Notifier] Gmail TLS (587) ile gonderildi")
        return
    except Exception as e:
        errors.append(f"TLS-587: {e}")
        print(f"[Notifier] TLS (587) basarisiz: {e}")

    # Hepsi basarisiz
    raise Exception(f"Tum yontemler basarisiz: {' | '.join(errors)}")


def send_daily_summary(
    trades_today: list,
    total_pnl: float,
    equity: float,
    regime: str,
):
    """Gun sonu ozet e-postasi."""
    if not is_enabled():
        return

    to_email = _get("NOTIFY_EMAIL")
    smtp_email = _get("SMTP_EMAIL") or to_email
    smtp_password = _get("SMTP_PASSWORD")
    # TR tarih formatı
    now = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%Y-%m-%d")
    sym = CURRENCY_SYMBOL

    trade_count = len(trades_today)
    pnl_color = "#22c55e" if total_pnl >= 0 else "#ef4444"
    pnl_sign = "+" if total_pnl >= 0 else ""

    trades_html = ""
    for t in trades_today[:10]:
        trades_html += f"""
        <tr>
            <td style="padding: 6px 8px; color: #e2e8f0;">{t.get('ticker','?')}</td>
            <td style="padding: 6px 8px; color: #e2e8f0;">{t.get('action','?').upper()}</td>
            <td style="padding: 6px 8px; color: #e2e8f0;">{t.get('qty',0)}</td>
            <td style="padding: 6px 8px; color: #e2e8f0;">{sym}{t.get('price',0):.2f}</td>
        </tr>"""

    subject = f"[BIST Warrior] Günlük Özet — {pnl_sign}{sym}{total_pnl:.2f} | {trade_count} işlem | {now}"

    html = f"""
    <div style="font-family: 'Segoe UI', Arial, sans-serif; max-width: 500px; margin: 0 auto; background: #0a0e17; color: #e2e8f0; border-radius: 12px; overflow: hidden; border: 1px solid #1e293b;">
        <div style="background: #1e293b; padding: 16px 24px;">
            <h2 style="margin: 0; color: white;">Günlük Özet — {now}</h2>
        </div>
        <div style="padding: 24px;">
            <div style="display: flex; gap: 16px; margin-bottom: 20px;">
                <div style="flex: 1; background: #111827; padding: 16px; border-radius: 8px; text-align: center;">
                    <p style="margin: 0; font-size: 12px; color: #94a3b8;">K/Z</p>
                    <p style="margin: 4px 0 0; font-size: 22px; font-weight: 700; color: {pnl_color};">{pnl_sign}{sym}{total_pnl:.2f}</p>
                </div>
                <div style="flex: 1; background: #111827; padding: 16px; border-radius: 8px; text-align: center;">
                    <p style="margin: 0; font-size: 12px; color: #94a3b8;">Öz Sermaye</p>
                    <p style="margin: 4px 0 0; font-size: 22px; font-weight: 700; color: #e2e8f0;">{sym}{equity:,.0f}</p>
                </div>
            </div>
            <p style="color: #94a3b8; font-size: 13px;">Rejim: <strong style="color: #e2e8f0;">{regime}</strong> | İşlem: <strong style="color: #e2e8f0;">{trade_count}</strong></p>
            <table style="width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 12px;">
                <tr style="border-bottom: 1px solid #1e293b;">
                    <th style="padding: 8px; text-align: left; color: #94a3b8;">Hisse</th>
                    <th style="padding: 8px; text-align: left; color: #94a3b8;">Aksiyon</th>
                    <th style="padding: 8px; text-align: left; color: #94a3b8;">Adet</th>
                    <th style="padding: 8px; text-align: left; color: #94a3b8;">Fiyat</th>
                </tr>
                {trades_html}
            </table>
            <p style="margin-top: 20px; font-size: 11px; color: #475569; text-align: center;">Meridian TR — BIST Otonom Trading Agent</p>
        </div>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_email
    msg["To"] = to_email
    msg.attach(MIMEText(f"Günlük Özet: {pnl_sign}{sym}{total_pnl:.2f} | {trade_count} işlem | Öz sermaye: {sym}{equity:,.0f}", "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        _send_email(smtp_email, smtp_password, to_email, msg)
        print(f"[Notifier] Gunluk ozet gonderildi: {subject}")
    except Exception as e:
        print(f"[Notifier] Ozet e-posta hatasi: {e}")


# ═══════════════════════════════════════════════════════════════
# ANALYST MODE — Sabah Özeti + Yüksek Güvenli Sinyal Alarmı
# ═══════════════════════════════════════════════════════════════

def send_morning_brief(scan_result: dict, regime_reasoning: str = "", subject_prefix: str = "[BIST Warrior]"):
    """
    Sabah 09:45 TRT öncesi, günün planını e-posta ile gönder.
    analyst mode için Midas'a kopyalanabilir format.
    """
    if not is_enabled():
        print("[Notifier] SMTP ayarsız, sabah özeti atlandı")
        return

    to_email = _get("NOTIFY_EMAIL")
    smtp_email = _get("SMTP_EMAIL") or to_email
    smtp_password = _get("SMTP_PASSWORD")
    sym = CURRENCY_SYMBOL

    # TR saat
    now_tr = datetime.now(timezone.utc) + timedelta(hours=3)
    date_str = now_tr.strftime("%d.%m.%Y")

    regime = (scan_result.get("regime") or "--").upper()
    strategy = scan_result.get("active_strategy", "--")
    decisions = scan_result.get("decisions", [])
    market_summary = scan_result.get("market_summary", "")

    # AL sinyalleri (conf ≥ 6)
    al_signals = [d for d in decisions if d.get("action") == "long" and d.get("confidence", 0) >= 6]
    watch_signals = [d for d in decisions if d.get("action") == "watch"]
    exit_signals = [d for d in decisions if d.get("action") in ("close_long", "reduce")]

    # Rejim rengi
    regime_color = {
        "BULL_STRONG": "#16a34a", "BULL": "#22c55e",
        "NEUTRAL": "#d4a349",
        "BEAR": "#ef4444", "BEAR_STRONG": "#991b1b"
    }.get(regime, "#8a8a8a")

    subject = f"{subject_prefix} 🌅 Bugünün Planı — {date_str} · {len(al_signals)} AL · Rejim: {regime}"

    # AL kartları HTML
    al_html = ""
    for i, d in enumerate(al_signals, 1):
        t = (d.get("ticker", "") or "").replace(".IS", "")
        al_html += f"""
        <div style="background:#fdfbf5;border:1px solid #e8dfc8;border-left:4px solid #E30A17;border-radius:6px;padding:14px;margin-bottom:10px">
          <div style="display:flex;justify-content:space-between;align-items:baseline">
            <div><strong style="font-size:16px">{i}. {t}</strong> <span style="color:#8a8a8a;font-size:11px">güven: <strong>{d.get('confidence', 0)}/10</strong></span></div>
            <span style="background:#fce8ea;color:#C8102E;padding:2px 8px;border-radius:3px;font-size:10px;font-weight:600">{(d.get('urgency','medium') or '').upper()}</span>
          </div>
          <table style="width:100%;margin-top:8px;font-size:12px">
            <tr><td style="color:#8a8a8a;padding:2px 0">Giriş</td><td style="text-align:right"><strong>{d.get('entry_zone', '--')} {sym}</strong></td></tr>
            <tr><td style="color:#8a8a8a;padding:2px 0">Stop-Loss</td><td style="text-align:right;color:#ef4444"><strong>{d.get('stop_loss', '--')} {sym}</strong></td></tr>
            <tr><td style="color:#8a8a8a;padding:2px 0">Hedef (TP)</td><td style="text-align:right;color:#22c55e"><strong>{d.get('take_profit', '--')} {sym}</strong></td></tr>
            <tr><td style="color:#8a8a8a;padding:2px 0">R/R Oranı</td><td style="text-align:right">{d.get('risk_reward', '--')}</td></tr>
            <tr><td style="color:#8a8a8a;padding:2px 0">Pozisyon</td><td style="text-align:right">%{d.get('position_size_pct', '--')} portföy</td></tr>
          </table>
          {f'<div style="margin-top:8px;padding-top:8px;border-top:1px dashed #e8dfc8;color:#5a5a5a;font-size:11px;line-height:1.5">{(d.get("reasoning") or "")[:220]}</div>' if d.get('reasoning') else ''}
        </div>"""

    if not al_html:
        al_html = '<div style="padding:20px;text-align:center;color:#8a8a8a">Bugün için güçlü alım sinyali bulunmuyor.</div>'

    html = f"""
    <div style="font-family:-apple-system,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;background:#faf6ee;color:#1a1a1a;border-radius:8px;overflow:hidden">
      <div style="background:linear-gradient(135deg,#E30A17 0%,#C8102E 100%);color:white;padding:24px">
        <div style="font-size:11px;letter-spacing:0.18em;opacity:0.9;text-transform:uppercase;margin-bottom:4px">🇹🇷 Meridian Capital Türkiye · Midas Destekli</div>
        <h1 style="margin:0;font-size:22px;font-weight:700">Bugünün Planı — {date_str}</h1>
        <div style="font-size:13px;opacity:0.9;margin-top:6px">Rejim: <strong>{regime}</strong> · Strateji: <strong>{strategy}</strong></div>
      </div>
      <div style="padding:22px">
        <div style="margin-bottom:18px">
          <div style="font-size:10px;letter-spacing:0.14em;color:#8a8a8a;text-transform:uppercase;margin-bottom:6px">📊 Piyasa Özeti</div>
          <div style="font-size:12px;line-height:1.6;color:#5a5a5a;padding:10px;background:#fdfbf5;border-left:3px solid {regime_color};border-radius:0 4px 4px 0">{market_summary or 'Özet yok'}</div>
        </div>

        <div style="font-size:10px;letter-spacing:0.14em;color:#8a8a8a;text-transform:uppercase;margin-bottom:10px">✅ AL Sinyalleri ({len(al_signals)})</div>
        {al_html}

        {f'''<div style="font-size:10px;letter-spacing:0.14em;color:#8a8a8a;text-transform:uppercase;margin:20px 0 8px">👁 Bekleme Listesi ({len(watch_signals)})</div>
        <div style="padding:10px;background:#faf1d9;border-radius:4px;font-size:12px">
          {'<br>'.join(f'<strong>{(w.get("ticker","") or "").replace(".IS","")}</strong> — {(w.get("reasoning","") or "")[:120]}' for w in watch_signals[:5])}
        </div>''' if watch_signals else ''}

        {f'''<div style="font-size:10px;letter-spacing:0.14em;color:#8a8a8a;text-transform:uppercase;margin:20px 0 8px">🚪 Çıkış Önerileri ({len(exit_signals)})</div>
        <div style="padding:10px;background:#fce8ea;border-radius:4px;font-size:12px">
          {'<br>'.join(f'<strong>{(x.get("ticker","") or "").replace(".IS","")}</strong> — {x.get("action","")}: {(x.get("reasoning","") or "")[:120]}' for x in exit_signals[:5])}
        </div>''' if exit_signals else ''}

        <div style="margin-top:24px;padding:14px;background:#f5f0e3;border-radius:6px;font-size:11px;color:#5a5a5a;text-align:center;line-height:1.5">
          📱 <strong>Midas'ta uygula:</strong> Hisse sembolünü, giriş fiyatını ve stop/hedef seviyelerini Midas'tan elle gir.<br>
          <span style="color:#8a8a8a;font-size:10px">Bu bir yatırım tavsiyesi değildir — AI analist önerisidir. Son karar sende.</span>
        </div>
      </div>
      <div style="background:#1a1a1a;color:#8a8a8a;padding:10px;text-align:center;font-size:10px;letter-spacing:0.12em">
        MERIDIAN CAPITAL TÜRKİYE · MIDAS DESTEKLİ · AI ANALİST
      </div>
    </div>
    """

    # Plain text fallback
    plain_lines = [
        f"Meridian Capital Türkiye — Bugünün Planı ({date_str})",
        f"Rejim: {regime} · Strateji: {strategy}",
        "",
        f"=== AL SİNYALLERİ ({len(al_signals)}) ===",
    ]
    for i, d in enumerate(al_signals, 1):
        t = (d.get("ticker", "") or "").replace(".IS", "")
        plain_lines.append(f"{i}. {t} (güven {d.get('confidence',0)}/10)")
        plain_lines.append(f"   Giriş: {d.get('entry_zone', '--')} {sym}")
        plain_lines.append(f"   Stop:  {d.get('stop_loss', '--')} {sym}")
        plain_lines.append(f"   Hedef: {d.get('take_profit', '--')} {sym}")
        plain_lines.append("")
    if not al_signals:
        plain_lines.append("(Bugün güçlü alım sinyali yok)")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_email
    msg["To"] = to_email
    msg.attach(MIMEText("\n".join(plain_lines), "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        _send_email(smtp_email, smtp_password, to_email, msg)
        print(f"[Notifier] 🌅 Sabah özeti gönderildi: {len(al_signals)} AL sinyali")
    except Exception as e:
        print(f"[Notifier] Sabah özeti hatası: {e}")


def send_high_conf_alert(decision: dict, market_price: float = 0):
    """
    Güven ≥ 9 olan sinyal için anlık alarm e-postası.
    """
    if not is_enabled():
        return

    to_email = _get("NOTIFY_EMAIL")
    smtp_email = _get("SMTP_EMAIL") or to_email
    smtp_password = _get("SMTP_PASSWORD")
    sym = CURRENCY_SYMBOL

    ticker = (decision.get("ticker", "") or "").replace(".IS", "")
    conf = decision.get("confidence", 0)
    action = (decision.get("action", "") or "").upper()
    now_tr = (datetime.now(timezone.utc) + timedelta(hours=3)).strftime("%H:%M TRT")

    subject = f"[BIST Warrior] 🚨 YÜKSEK GÜVEN: {action} {ticker} — {conf}/10"

    html = f"""
    <div style="font-family:-apple-system,'Segoe UI',sans-serif;max-width:500px;margin:0 auto;background:#ffffff;border:2px solid #E30A17;border-radius:8px;overflow:hidden">
      <div style="background:#E30A17;color:white;padding:16px 20px">
        <div style="font-size:11px;letter-spacing:0.14em;opacity:0.9">🚨 YÜKSEK GÜVENLİ SİNYAL · {now_tr}</div>
        <h2 style="margin:4px 0 0;font-size:22px">{action} {ticker}</h2>
        <div style="font-size:12px;opacity:0.9;margin-top:4px">Güven: <strong>{conf}/10</strong> · {decision.get('strategy', 'momentum')}</div>
      </div>
      <div style="padding:20px">
        <table style="width:100%;font-size:13px">
          <tr><td style="color:#8a8a8a;padding:4px 0">Giriş</td><td style="text-align:right"><strong>{decision.get('entry_zone', '--')} {sym}</strong></td></tr>
          <tr><td style="color:#8a8a8a;padding:4px 0">Stop-Loss</td><td style="text-align:right;color:#ef4444"><strong>{decision.get('stop_loss', '--')} {sym}</strong></td></tr>
          <tr><td style="color:#8a8a8a;padding:4px 0">Hedef</td><td style="text-align:right;color:#22c55e"><strong>{decision.get('take_profit', '--')} {sym}</strong></td></tr>
          <tr><td style="color:#8a8a8a;padding:4px 0">R/R</td><td style="text-align:right">{decision.get('risk_reward', '--')}</td></tr>
          {f'<tr><td style="color:#8a8a8a;padding:4px 0">Güncel</td><td style="text-align:right">{market_price:.2f} {sym}</td></tr>' if market_price > 0 else ''}
        </table>
        {f'<div style="margin-top:12px;padding:10px;background:#fce8ea;border-radius:4px;font-size:11px;color:#5a5a5a;line-height:1.5">{decision.get("reasoning", "")[:250]}</div>' if decision.get('reasoning') else ''}
        <div style="margin-top:16px;padding:10px;background:#f5f0e3;border-radius:4px;font-size:10px;color:#8a8a8a;text-align:center">
          📱 Midas'a elle gir · Son karar sende
        </div>
      </div>
    </div>
    """

    plain = (
        f"🚨 YÜKSEK GÜVENLİ SİNYAL — {now_tr}\n"
        f"{action} {ticker} (güven {conf}/10)\n"
        f"Giriş: {decision.get('entry_zone', '--')} {sym}\n"
        f"Stop:  {decision.get('stop_loss', '--')} {sym}\n"
        f"Hedef: {decision.get('take_profit', '--')} {sym}\n"
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_email
    msg["To"] = to_email
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    try:
        _send_email(smtp_email, smtp_password, to_email, msg)
        print(f"[Notifier] 🚨 Yüksek güven alarmı: {action} {ticker} ({conf}/10)")
    except Exception as e:
        print(f"[Notifier] Alarm hatası: {e}")
