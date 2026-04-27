"""
claude_brain.py — Otonom AI Trading Agent Beyni (Meridian TR V1)

BIST (Borsa Istanbul) için uyarlandı:
  - Prompt Türkçeye çevrildi, dil ve bağlam TR finans piyasası
  - Para birimi: TRY (₺)
  - Benchmark: XU100 (BIST 100 endeksi)
  - Short yasak (BIST'te açığa satış sınırlı → prompt bunu empoze eder)
  - PDT yok (TR'de day-trade limiti yok)
  - Pre-market/after-hours yok (BIST'te sadece 10:00-18:00 TRT seansı)

Karar döngüsü:
  1. Piyasa rejimini belirle (boğa/ayı/yatay — XU100 + genel momentum)
  2. Rejime uygun stratejiyi seç (momentum / selektif swing / defansif)
  3. Her hisse için multi-step analiz (trend + momentum + MACD + BB + katalizor)
  4. Risk/ödül + güven skoru
  5. Aksiyonable kararları gerekçeleriyle birlikte döndür
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import anthropic
from dotenv import dotenv_values, load_dotenv

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)
_env_vals = dotenv_values(_env_path)

def _get(key): return os.getenv(key) or _env_vals.get(key, "")

client = anthropic.Anthropic(api_key=_get("ANTHROPIC_API_KEY"))
from config import AI_MODEL, CURRENCY_SYMBOL, CURRENCY, BENCHMARK, SHORT_ENABLED, OPERATION_MODE
MODEL = AI_MODEL

# ─────────────────────────────────────────────────────────────────
# Ana Karar Motoru
# ─────────────────────────────────────────────────────────────────

def run_brain(
    market_data: dict,
    portfolio: dict,
    recent_trades: list,
    auto_execute: bool = False,
) -> dict:
    """
    Claude Otonom Karar Motoru — BIST V1

    Returns:
        {
          "decisions": [{
            "ticker": "GARAN.IS",
            "action": "long",
            "confidence": 8,
            "strategy": "momentum",
            "reasoning": "...",
            "entry_zone": "42.50-43.00",
            "stop_loss": "41.80",
            "take_profit": "44.50",
            "risk_reward": "1:2.5",
            "position_size_pct": 1.5,
            "urgency": "high",
            "risk_note": "..."
          }],
          "regime": "bull|bear|neutral",
          "regime_reasoning": "...",
          "active_strategy": "momentum|selective_swing|defensive|mean_reversion",
          "market_summary": "...",
          "portfolio_note": "...",
          "watchlist_alerts": [...],
          "timestamp": "..."
        }
    """
    if not _get("ANTHROPIC_API_KEY"):
        return _empty("API anahtari eksik")

    if "error" in market_data:
        return _empty(f"Piyasa verisi alinamadi: {market_data['error']}")

    meta = market_data.get("_meta", {})
    market_open = meta.get("market_open", False)
    detected_regime = meta.get("regime", "unknown")
    benchmark_change = meta.get("benchmark_change", 0)
    stage1_passed = meta.get("stage1_passed", 0)
    stage2_analyzed = meta.get("stage2_analyzed", 0)
    universe_size = meta.get("universe_size", 0)

    # Kantitatif rejim + anomali context (opsiyonel)
    quant_context = ""
    try:
        from regime_detector import detect_regime
        quant = detect_regime(market_data)
        quant_context += f"\n## KANTITATIF REJIM ANALIZI\n"
        quant_context += f"Quant Rejim: {quant['regime']} (skor: {quant['quant_score']}/100, güven: {quant['confidence']}%)\n"
        quant_context += f"Analiz: {quant['reasoning']}\n"
        if quant['regime'] != detected_regime and detected_regime != "unknown":
            quant_context += f"NOT: Kantitatif rejim ({quant['regime']}) teknik rejimden ({detected_regime}) farklı — ayrışmayı araştır.\n"
    except Exception:
        pass

    try:
        from anomaly_detector import detect_anomalies
        anomalies = detect_anomalies(market_data)
        if anomalies.get("anomaly_count", 0) > 0:
            quant_context += f"\n## ANOMALI UYARILARI\n"
            quant_context += f"Risk Seviyesi: {anomalies['risk_level'].upper()} — {anomalies['anomaly_count']} anomali tespit edildi\n"
            for a in anomalies["anomalies"][:5]:
                quant_context += f"  - [{a['severity'].upper()}] {a['ticker']}: {a['detail']}\n"
    except Exception:
        pass

    positions_text = _format_positions(portfolio)
    market_text    = _format_market_data(market_data)
    trades_text    = _format_recent_trades(recent_trades)
    ranking_text   = _format_momentum_ranking(market_data)

    try:
        from trade_journal import get_learning_context
        learning_context = get_learning_context(limit=5)
    except Exception:
        learning_context = ""

    cash   = portfolio.get("cash", 0)
    equity = portfolio.get("equity", 0)

    # Günlük plan bağlamı — AI bugünün bütçesini ve max işlem sayısını bilsin
    daily_plan_context = ""
    try:
        import midas_journal as _midas
        plan = _midas.get_daily_plan()
        if plan.get("user_status") in ("approved", "adjusted"):
            # Bugünkü açılan işlem sayısını öğren
            report = _midas.compute_daily_report()
            opened = report.get("opened_today", {}).get("count", 0)
            remaining_trades = max(0, (plan.get("max_trades", 0) or 0) - opened)
            budget_used = report.get("budget", {}).get("used_try", 0)
            budget_remaining = max(0, (plan.get("daily_budget_try", 0) or 0) - budget_used)
            pnl = report.get("realized", {}).get("pnl_try", 0)
            goal_progress = report.get("goal", {}).get("progress_pct", 0)

            daily_plan_context = f"""

## 🎯 BUGÜNÜN ONAYLANMIŞ PLANI (AI → Kullanıcı Onaylı)
Gün Kalitesi: **{plan.get('day_quality','?').upper()}**
Günlük Bütçe: ₺{plan.get('daily_budget_try',0):,.0f}  (kullanılan: ₺{budget_used:,.0f} · KALAN: ₺{budget_remaining:,.0f})
Kâr Hedefi: ₺{plan.get('daily_profit_target_try',0):,.0f}  (gerçekleşen: ₺{pnl:,.0f} · ilerleme: %{goal_progress:.0f})
Max İşlem: {plan.get('max_trades',0)}  (bugün açılan: {opened} · KALAN HAK: {remaining_trades})
Kredili Aktif: {'✓ EVET (gün-içi kapanmalı)' if plan.get('credit_enabled') else '✗ HAYIR (sadece nakit)'}

⚠️ ZORUNLU DİSİPLİN:
  - Bütçe üstünde toplam öneri verme — tüm AL kararlarının toplam maliyeti ₺{budget_remaining:,.0f}'yi AŞMASIN
  - Max işlem hakkı {remaining_trades} — bu sayıdan fazla 'long' action VERME
  - Hedef doldu ise yeni sinyal üretme, action='watch' kullan (kâr realize → dur)
  - Kredili aktif değilse KREDİLİ önerisi YOK
"""
    except Exception:
        pass

    prompt = _build_master_prompt(
        cash=cash,
        equity=equity,
        positions_text=positions_text,
        market_text=market_text,
        trades_text=trades_text,
        ranking_text=ranking_text,
        detected_regime=detected_regime,
        benchmark_change=benchmark_change,
        market_open=market_open,
        auto_execute=auto_execute,
        learning_context=learning_context,
        quant_context=quant_context + daily_plan_context,
        stage1_passed=stage1_passed,
        stage2_analyzed=stage2_analyzed,
        universe_size=universe_size,
    )

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=8192,
            messages=[{"role": "user", "content": prompt}]
        )

        text = message.content[0].text.strip()
        result = _extract_json(text)

        # BIST short yasağı — Claude yanlışlıkla short önerirse filtrele
        if not SHORT_ENABLED:
            filtered = []
            for d in result.get("decisions", []):
                if d.get("action") == "short":
                    d["action"] = "watch"
                    d["reasoning"] = "BIST'te short yasak → gözlem. " + d.get("reasoning", "")
                filtered.append(d)
            result["decisions"] = filtered

        result["timestamp"]    = datetime.now(timezone.utc).isoformat()
        result["auto_execute"] = auto_execute
        result["market_open"]  = market_open
        result["market_data_snapshot"] = {
            k: {
                "price": v.get("price"),
                "signal": v.get("signal"),
                "momentum_score": v.get("momentum_score"),
                "rsi14": v.get("rsi14"),
                "volume_ratio": v.get("volume_ratio"),
                "trend": v.get("trend"),
                "macd": v.get("macd"),
                "macd_histogram": v.get("macd_histogram"),
                "macd_cross": v.get("macd_cross"),
                "bb_position": v.get("bb_position"),
                "bb_width": v.get("bb_width"),
                "in_focus": v.get("in_focus", False),
            }
            for k, v in market_data.items()
            if isinstance(v, dict) and not k.startswith("_")
        }
        return result

    except Exception as e:
        return _empty(f"Claude hatasi: {str(e)[:200]}")


# ─────────────────────────────────────────────────────────────────
# Master Prompt — Türkçe, BIST Bağlamı
# ─────────────────────────────────────────────────────────────────

def _build_master_prompt(
    cash, equity, positions_text, market_text,
    trades_text, ranking_text, detected_regime, benchmark_change,
    market_open, auto_execute,
    learning_context="", quant_context="",
    stage1_passed=0, stage2_analyzed=0, universe_size=0,
) -> str:
    market_status = "AÇIK" if market_open else "KAPALI (analiz/hazırlık modu)"
    sym = CURRENCY_SYMBOL
    learning_section = learning_context if learning_context else ""
    short_rule = "AÇIK — short kararları üretebilirsin" if SHORT_ENABLED else "YASAK — BIST'te açığa satış sınırlı, asla 'short' önerme (gözlem 'watch' olarak işaretle)"

    analyst_banner = ""
    if OPERATION_MODE == "analyst":
        analyst_banner = """
═══════════════ BIST WARRIOR · ANALYST MODU (Midas Destekli) ═══════════════
Bu bir OTOMATIK emir sistemi DEĞİL. Sen AI Baş Analist'sin.
Kullanıcı senin önerilerini görecek, Midas'tan ELLE **LİMİT EMİR** girecek.
Platform: BIST Warrior v1 · Midas aracılığıyla manuel BIST trading

🎯 KRİTİK STRATEJİ KURALLARI (her sinyal için MUTLAK uygula):

  1. DEFAULT ORDER TYPE: **"limit"** — piyasa emri değil
     (Midas'ta limit emir kullanıcı gün içi dolum için planlanır, slippage'ı azaltır)

  2. **ASLA TEPEDEN GİRME — entry_zone CURRENT'tan AŞAĞIDA olmalı**:
     • entry_high ≤ current_price × 0.999 (en fazla %0.1 yukarısı tolerans)
     • entry_low  ≥ current_price × 0.97  (en fazla %3 aşağısı, daha derinse alım fırsatı kaçar)
     • Sweet spot: entry_zone = current × 0.985 ila current × 0.995 (yani %0.5-1.5 düşüş bekle)
     • Yüksek momentum/güçlü trend ise entry_zone = current × 0.992-0.998 (sığ pullback)
     • Yatay/zayıf trendde entry_zone = current × 0.97-0.985 (derin pullback)
     • Kullanıcı bu sinyali GÖRÜNCEYE kadar geçen ~10 dk içinde fiyat senin entry'nin üstüne çıkarsa SİNYAL ÖLÜR
       — bu yüzden entry'yi current'a YAKIN tut ama ASLA üstte verme

  3. **TP GERÇEKÇİLİĞİ — "geçmiş kapanıştan kazanç" yasak**:
     • TP, current_price × 1.015 ile current_price × 1.04 arasında olmalı (intraday alpha)
     • Önceki gün kapanışını veya bugün açılış gap'ini TP olarak SAYMA
     • TP - current_price farkı = SAFI INTRADAY ALPHA — bu rakam %1.5'tan az ise sinyal verme
     • Örnek YANLIŞ: dün ₺100 kapanış, bugün ₺102 açılış, TP=₺103 → safi alpha sadece %1, ÇIKARMA
     • Örnek DOĞRU: bugün ₺102 açılış, current ₺101.5 (gap doldu), TP=₺104 → safi alpha %2.5, OK

  4. **STOP-LOSS DİSİPLİNİ**:
     • SL = current × 0.985 ile current × 0.97 arası (entry'nin 0.5-1.5% altı)
     • ATR_pct ile teyit: SL mesafesi en az 1.0× ATR_pct olmalı (gürültü tetiklemesin)
     • SL > entry yasak (long sinyalde anlamsız)

  5. **R/R DİSİPLİNİ**:
     • (TP - entry) / (entry - SL) ≥ 1.5 → minimum
     • (TP - entry) / (entry - SL) ≥ 2.0 → tercih
     • R/R < 1.5 ise action="watch" yap, sinyal verme

  6. **KREDİLİ SADECE 9-10 güven + <4 saat tutma süresi** (gün-içi kapanacak)

  7. **YANIT'A current_price_at_signal ekle (ZORUNLU)**:
     Her decision için JSON'da `current_price_at_signal` alanı olmalı —
     bu, sinyal verirken piyasada gördüğün fiyat. Bu alanın değeri
     market_data tablosundaki `price` ile eşleşmeli. Server bu alanla
     entry/TP/SL'in geçerli olup olmadığını sonradan doğrular.

⏱ GÜN-İÇİ TRADER ZİHNİYETİ (mutlak kural):
  • Bu sistem **day-trader**'dır — tüm pozisyonlar seans bitmeden (17:55 TRT) KAPATILACAK
  • Overnight taşıma YASAK. "Yarın bakarız" yaklaşımı YOK
  • Her sinyal için `expected_exit` (HH:MM, TRT) ve `max_hold_minutes` belirt
  • Tipik holding: 30-180 dk. Momentum güçlüyse 60-90 dk hedefle
  • Kapanışa <60 dk kala yeni long açma (`urgency="low"` yaz, "watch" tercih et)
  • Entry zone geçildiyse KOVALAMA → bir sonraki setup'ı bekle
  • "Yap-çık-yap-çık" döngüsü: aynı hisseye gün içinde 2-3 kez dönebilirsin (farklı sinyal)

📋 KULLANICI AKIŞI:
  1. Sen sinyal üret → 2. Gemini Council teyit → 3. Kullanıcı Midas'a LİMİT EMRİ ver
  4. Limit doldu → kullanıcı "YAPTIM" tıklar → sistem pozisyon izler
  5. TP/SL yakın → sistem uyarır → kullanıcı Midas'ta elle çıkar

⚠️ BU NEDENLE:
  • Her sinyal Midas limit emri için HAZIR olsun (ticker, qty, limit_price, SL, TP)
  • Güven 9-10 ise anlık alarm
  • Belirsiz → action="watch"
  • Aç gözlü olma — bütçeyi aşma, max işlem sayısını geçme
═══════════════════════════════════════════════════════════════════════════
"""

    return f"""Sen, otonom bir AI hedge fund'ın BIST (Borsa İstanbul) Baş Trader'ısın.
Sen bir indikatör yorumcusu DEĞİL, icra yetkili karar vericisisin.
{analyst_banner}
Görevin:
1. ÖNCE piyasa rejimini belirle (boğa/ayı/yatay) — her şey buna göre şekillenir
2. Rejime uygun optimal stratejiyi seç
3. Her hisseyi multi-step akıl yürütmeyle analiz et (sadece indikatör değil)
4. Giriş/çıkış seviyeleri ve pozisyon boyutuyla spesifik, uygulanabilir kararlar ver
5. Gerekçelerini bir fon yöneticisinin yatırımcılarına açıklar gibi yaz

## MEVCUT DURUM
Piyasa Durumu: {market_status}  (BIST 10:00-18:00 TRT, Pzt-Cum)
Tespit Edilen Rejim: {detected_regime}
BIST 100 (XU100) Günlük Değişim: {benchmark_change:+.2f}%
Nakit: {sym}{cash:,.2f}
Toplam Öz Sermaye: {sym}{equity:,.2f}
Para Birimi: {CURRENCY}
Short Pozisyon Kuralı: {short_rule}

## TARAMA KAPSAMI
Evren boyutu: {universe_size} BIST hissesi
Evre 1 prefilter'ı geçen: {stage1_passed}
Evre 2 derin analiz edilen: {stage2_analyzed}
(Not: `in_focus=True` etiketli hisseler Focus List üyesi, diğerleri wide-scan'den adaylar)

## AÇIK POZİSYONLAR
{positions_text}

## PİYASA VERİSİ (tüm teknik indikatörlerle)
{market_text}

## MOMENTUM SIRALAMASI (skora göre, en yüksekten)
{ranking_text}

## SON İŞLEM GEÇMİŞİ
{trades_text}
{learning_section}
{quant_context}

## STRATEJİ ÇERÇEVESİ

### Rejim → Strateji Eşleşmesi:
- **BOĞA piyasası**: MOMENTUM stratejisi (Ross Cameron Gap & Go — BIST uyarlaması)
  - Hedef: gap_pct > 2%, volume_ratio > 1.5×, strong_uptrend hisseler
  - Giriş: VWAP veya EMA9 desteğine geri çekilmede
  - Çıkış: EMA9 trailing stop, min 1:2 R/R ile kâr realize et
  - BIST'te tavan/taban %10 — tavandan önce kısmi kâr realize edilmeli

- **YATAY piyasa**: SELEKTİF SWING stratejisi
  - Hedef: sadece momentum_score > 70 hisseler
  - Giriş: kritik destek seviyelerinde, hacim teyidiyle
  - Küçük pozisyon boyutları (risk %50 azalt)
  - Likit bankacılık/holding tercih (ETF yerine)

- **AYI piyasası**: DEFANSİF stratejisi
  - Mevcut long pozisyonları kapat veya azalt
  - >60% nakitte kal
  - BIST'te short yasak → sadece cash korunma, gerekirse koruma likiditesi (BIST30 ETF gibi)

### Ross Cameron Momentum Kriterleri (BIST uyarlaması):
1. Gün içi gap > 4% + katalizor varsa (bilanço, sektör haberi, TCMB kararı)
2. Relatif hacim > 2× ortalama
3. Float rotasyonu (düşük float'lı küçük kap hisseler — BIST'te yaygın)
4. İlk VWAP pullback = ideal giriş
5. KOVALAMA YOK — giriş kaçırıldıysa sonraki setup'ı bekle
6. BIST tavan limiti %10 — tavana yakın girişlerde R/R zayıflar

### Multi-Step Analiz (HER KARAR İÇİN ZORUNLU):
Her ticker için iç muhasebe:
1. TREND nedir? (EMA yapısı: 9>21>50 = güçlü yükseliş)
2. MOMENTUM nedir? (RSI bandı, hacim teyidi, MACD histogram yönü)
3. MACD ne diyor? (bullish_cross = al sinyali, bearish_cross = sat, büyüyen histogram = ivme artıyor)
4. BOLLINGER ne diyor? (BB_Pos<0.2 = dipten dönüş fırsatı, BB_Pos>0.8 = tepe riski, BB_Width yüksek = breakout potansiyeli)
5. RİSK nerede? (ATR tabanlı stop, kritik destek/direnç, BB alt bandı destek olarak)
6. KATALİZOR ne? (neden hareket ediyor — bilanço mu, sektör haberi mi, makro mu?)
7. Rejime UYUYOR mu? (ayı piyasasında long alma)
8. R/R oranı nedir? (minimum 1:2, tercihen 1:3)

### Sinyal Confluence (birden fazla teyit şart):
- MACD bullish cross + RSI 40-65 bandı + uptrend = YÜKSEK güven
- MACD bearish cross + RSI > 70 + downtrend = SAT sinyali
- Bollinger squeeze (düşük BB_Width) → genişleme = breakout yakın
- Alt BB'de fiyat + bullish MACD = mean reversion alım
- Üst BB'de fiyat + MACD negatif uyumsuzluk = potansiyel geri çekilme

### BIST'e Özgü Notlar:
- Bankalar (AKBNK, GARAN, ISCTR…) genelde birlikte hareket eder — birbirini teyit eder ama sektör aşırı konsantrasyon riski var
- TCMB faiz kararı günlerinde volatilite patlar → risk düşür
- Enflasyon ve kur hareketleri tüm hisseleri etkiler — TRY zayıflaması ihracatçılar (ARCLK, VESTL, FROTO) için pozitif
- Tavan/taban limiti %10 — tavan/taban yapan hisseler bir sonraki güne taşınabilir ama hacim kaybeder
- Likidite değişkendir — Focus List dışındaki adaylarda spread geniş olabilir

## RİSK KURALLARI (MUTLAK — ASLA İHLAL ETME)
1. İşlem başına ASLA %2'den fazla risk alma
2. Tek pozisyona ASLA all-in olma (max %15 portföy)
3. Rejim = ayı ise max %40 yatırımda, %60 nakit
4. Her girişten ÖNCE stop-loss planı olmalı
5. Piyasa KAPALIYSA: analiz yap, watchlist hazırla, anlık uygulama önerisi YAPMA
6. BIST'te short yasak — {"short kararı çıkarsa action='watch' olarak işaretle" if not SHORT_ENABLED else "short kullanılabilir ama dikkat"}
7. Tek sektör ağırlığı %50'yi geçmesin (bankacılık BIST'te ağır — dikkat)

## POZİSYON BOYUTLANDIRMA
- Güven 8-10: portföy riskinin %2.0'a kadarı
- Güven 6-7: %1.5'a kadar
- Güven 4-5: %1.0'a kadar
- Güven 1-3: İŞLEM YOK — sadece gözlem

## YANIT FORMATI
SADECE bu JSON yapısıyla yanıtla. Başka metin, markdown, JSON dışı açıklama YOK:
{{
  "regime": "bull | bear | neutral",
  "regime_reasoning": "2-3 cümle: veriye göre neden bu rejim?",
  "active_strategy": "momentum | selective_swing | defensive | mean_reversion",
  "decisions": [
    {{
      "ticker": "GARAN.IS",
      "action": "long | close_long | hold | watch | reduce",
      "confidence": 8,
      "strategy": "momentum",
      "reasoning": "Multi-step: (1) Güçlü yükseliş EMA9>21>50, (2) RSI 58 ideal bölge, (3) Vol oranı 2.1× kurumsal alım, (4) Bankacılık sektörü pozitif TCMB sinyalinden, (5) Boğa rejimiyle uyumlu. VWAP pullback'te giriş.",
      "current_price_at_signal": 43.20,
      "entry_zone": "42.50-43.00",
      "stop_loss": "41.80",
      "take_profit": "44.50",
      "risk_reward": "1:2.5",
      "position_size_pct": 1.5,
      "urgency": "high | medium | low",
      "expected_exit": "14:30",
      "max_hold_minutes": 90,
      "risk_note": "Yarın TCMB PPK toplantısı — volatilite artabilir"
    }}
  ],
  "market_summary": "3-4 cümle: rejim + genel BIST durumu + önemli sektörel hareketler",
  "portfolio_note": "2-3 cümle: portföy sağlığı, ayarlama önerisi, nakit dağılımı",
  "watchlist_alerts": [
    {{"ticker": "THYAO.IS", "alert": "165₺ direncinde breakout yaklaşıyor, hacim patlaması bekle"}}
  ]
}}

ÖNEMLİ YANIT KURALLARI:
- EN AZ 8, EN FAZLA 12 en alakalı ticker (düşük ilgili hold'ları atla)
- `confidence` 1-10 tam sayı
- `reasoning` 1-2 cümle, ana faktörlerle
- `entry_zone`, `stop_loss`, `take_profit`: spesifik TL fiyat seviyeleri
- Piyasa KAPALIYSA: `urgency="low"`, sonraki seans planı yaz
- JSON'u KOMPAKT tut — fazla whitespace/açıklama YOK
- {"SHORT kararı ÜRETME" if not SHORT_ENABLED else "SHORT kullanılabilir"}"""


# ─────────────────────────────────────────────────────────────────
# Formatlama Yardımcıları
# ─────────────────────────────────────────────────────────────────

def _format_positions(portfolio: dict) -> str:
    positions = portfolio.get("positions", [])
    sym = CURRENCY_SYMBOL
    if not positions:
        return "  Açık pozisyon yok (%100 nakit)"
    lines = []
    for p in positions:
        pl = p.get("unrealized_pl", 0)
        pl_sign = "+" if pl >= 0 else ""
        lines.append(
            f"  {p['ticker']}: {p['qty']} hisse @ {sym}{p.get('avg_entry', p.get('avg_entry_price', 0)):.2f} "
            f"| Şu an {sym}{p['current_price']:.2f} | K/Z {pl_sign}{sym}{pl:.2f}"
        )
    return "\n".join(lines)


def _format_market_data(market_data: dict) -> str:
    """
    Market veriyi Claude için kompakt formatta serialize et.
    Çok uzamaması için momentum'a göre sırala, top 60'ı göster.
    """
    sym = CURRENCY_SYMBOL
    tickers = []
    for ticker, d in market_data.items():
        if not isinstance(d, dict) or ticker.startswith("_"):
            continue
        if "price" not in d:
            continue
        tickers.append((ticker, d))
    # Momentum skoru yüksekten düşüğe
    tickers.sort(key=lambda x: x[1].get("momentum_score", 50), reverse=True)

    lines = []
    for ticker, d in tickers[:60]:
        focus_tag = " [FOCUS]" if d.get("in_focus") else ""
        macd_info = f"MACD={d.get('macd',0)} Hist={d.get('macd_histogram',0)} {d.get('macd_cross','')}"
        bb_info = f"BB_Pos={d.get('bb_position','?')} BB_W={d.get('bb_width','?')}"
        lines.append(
            f"  {ticker}{focus_tag}: {sym}{d['price']} ({d['change_pct']:+.1f}%) "
            f"Gap:{d.get('gap_pct',0):+.1f}% "
            f"| EMA9={d['ema9']} EMA21={d['ema21']} EMA50={d.get('ema50','?')} "
            f"| RSI={d['rsi14']} ATR%={d.get('atr_pct','?')} "
            f"| Vol={d.get('volume_ratio',0):.1f}x "
            f"| VWAP={sym}{d.get('vwap','?')} "
            f"| {macd_info} | {bb_info} "
            f"| Trend: {d.get('trend','?')} Sinyal: {d['signal']} "
            f"| Momentum: {d.get('momentum_score',50)}"
        )
    if len(tickers) > 60:
        lines.append(f"  ... ({len(tickers)-60} daha az öncelikli hisse atlandı)")
    return "\n".join(lines) or "  Piyasa verisi yok"


def _format_recent_trades(recent_trades: list) -> str:
    sym = CURRENCY_SYMBOL
    if not recent_trades:
        return "  Son işlem geçmişi yok"
    lines = []
    for t in recent_trades[:10]:
        lines.append(
            f"  {t.get('timestamp','')[:16]} | {t.get('ticker')} "
            f"{t.get('action')} @ {sym}{t.get('price','?')} -> {t.get('status')}"
        )
    return "\n".join(lines)


def _format_momentum_ranking(market_data: dict) -> str:
    """Momentum skoruna göre sıralı liste — top 25."""
    stocks = []
    for ticker, d in market_data.items():
        if not isinstance(d, dict) or ticker.startswith("_"):
            continue
        if "momentum_score" in d:
            stocks.append((
                ticker, d["momentum_score"], d.get("signal", "?"),
                d.get("change_pct", 0), d.get("in_focus", False)
            ))

    stocks.sort(key=lambda x: x[1], reverse=True)
    lines = []
    for i, (ticker, score, signal, change, in_focus) in enumerate(stocks[:25], 1):
        bar = "█" * (score // 10) + "░" * (10 - score // 10)
        tag = "F" if in_focus else "+"
        lines.append(f"  #{i:2d} [{tag}] {ticker:14s}: {score:3d}/100 [{bar}] Sinyal={signal:12s} ({change:+6.2f}%)")
    return "\n".join(lines) or "  Sıralama verisi yok"


# ─────────────────────────────────────────────────────────────────
# JSON Çıkartma
# ─────────────────────────────────────────────────────────────────

def _extract_json(text: str) -> dict:
    """Claude çıktısından JSON bloğu çıkarır — hata toleranslı.

    Strateji:
      1. ```json ... ``` code block varsa al
      2. İlk '{' den son '}' ye kırp
      3. Strict parse
      4. Trailing comma temizle + retry
      5. Kesilmiş (truncated) JSON için bracket balance + retry
    """
    import re

    code_block = re.search(r'```(?:json)?\s*\n?(.*?)```', text, re.DOTALL)
    if code_block:
        text = code_block.group(1).strip()

    start = text.find("{")
    end   = text.rfind("}") + 1
    if start != -1 and end > start:
        text = text[start:end]

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    cleaned = re.sub(r',\s*([}\]])', r'\1', text)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    return json.loads(_balance_json(cleaned))


def _balance_json(text: str) -> str:
    """Kesilmiş JSON'u tamir eder: açık string'i kapat, eksik ']'/'}' ekle,
    son virgülü temizle. Claude max_tokens'a dayandığında truncate olan
    response'ları kurtarmak için."""
    i = 0
    in_string = False
    escape = False
    stack: list[str] = []  # '{' ve '[' takibi
    last_valid = 0

    while i < len(text):
        ch = text[i]
        if escape:
            escape = False
        elif ch == "\\" and in_string:
            escape = True
        elif ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch == "{" or ch == "[":
                stack.append(ch)
            elif ch == "}" or ch == "]":
                if stack:
                    stack.pop()
            if not in_string and ch in ",}]" and not stack[:1] == [None]:
                last_valid = i + 1
        i += 1

    # Açık string'i kapat
    if in_string:
        text = text[:last_valid] if last_valid else text + '"'
        stack_copy = stack[:]
    else:
        stack_copy = stack[:]

    # Trailing comma temizle (son anlamlı karakter virgülse)
    stripped = text.rstrip()
    while stripped.endswith(","):
        stripped = stripped[:-1].rstrip()
    text = stripped

    # Eksik kapanışları tamamla (ters sırayla)
    closers = {"{": "}", "[": "]"}
    for opener in reversed(stack_copy):
        text += closers[opener]

    return text


# ─────────────────────────────────────────────────────────────────
# Post-Trade Review (Öğrenme Döngüsü — TR)
# ─────────────────────────────────────────────────────────────────

def review_past_trades(recent_trades: list, portfolio: dict) -> dict:
    """
    Son işlemleri analiz eder, öğrenme çıkarımları üretir.
    Her işlem kapandıktan sonra: "Neden kazandım/kaybettim?" analizi.
    """
    if not _get("ANTHROPIC_API_KEY") or not recent_trades:
        return {"review": "Analiz için yeterli veri yok", "lessons": []}

    trades_text = _format_recent_trades(recent_trades)
    sym = CURRENCY_SYMBOL

    prompt = f"""Sen, kendi geçmiş BIST işlem kararlarını inceleyen, kendini geliştiren bir AI trader'sın.

## SON İŞLEMLER
{trades_text}

## MEVCUT PORTFÖY
Nakit: {sym}{portfolio.get('cash', 0):,.2f}
Öz Sermaye: {sym}{portfolio.get('equity', 0):,.2f}

## GÖREVİN
Her işlemi analiz et ve şunları ver:
1. Ne DOĞRU gitti (bu kalıpları tekrarla)
2. Ne YANLIŞ gitti (bu kalıplardan kaçın)
3. Gelecekteki işlemler için spesifik dersler
4. Kazanma oranı tahmini ve risk-düzeltilmiş performans

Sadece JSON ile yanıt ver:
{{
  "overall_grade": "A/B/C/D/F",
  "win_rate_estimate": "60%",
  "lessons": [
    {{"type": "positive", "lesson": "..."}},
    {{"type": "negative", "lesson": "..."}}
  ],
  "strategy_adjustments": ["..."],
  "risk_assessment": "Çok mu az mı risk alıyoruz?"
}}"""

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        return _extract_json(text)
    except Exception as e:
        return {"review": f"Review hatasi: {str(e)[:100]}", "lessons": []}


# ─────────────────────────────────────────────────────────────────
# Yardımcılar
# ─────────────────────────────────────────────────────────────────

def _empty(reason: str) -> dict:
    return {
        "decisions": [],
        "regime": "unknown",
        "regime_reasoning": reason,
        "active_strategy": "none",
        "market_summary": reason,
        "portfolio_note": "",
        "watchlist_alerts": [],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "auto_execute": False,
        "error": reason,
    }


def pdt_trades_left(recent_trades: list) -> int:
    """
    BIST'te PDT (Pattern Day Trader) kuralı YOK — her zaman sınırsız.
    US versiyonuyla uyumluluk için eski imza korundu.
    """
    return 99


# ═══════════════════════════════════════════════════════════════
# 🌅 AI DAILY BRIEFING — Gün başı risk yöneticisi
# ═══════════════════════════════════════════════════════════════

def generate_daily_plan(market_data: dict, portfolio: dict, last_trades: list = None) -> dict:
    """
    Gün başı AI Briefing — Claude global+lokal duruma bakıp günün planını önerir.

    Çıktı:
        {
          "day_quality": "excellent|good|normal|risky|skip",
          "daily_budget_try": 250000,
          "daily_profit_target_try": 5000,
          "max_trades": 8,
          "credit_enabled": true,
          "reasoning": "Bankacılık sektörü güçlü, haberler pozitif...",
          "key_risks": ["TCMB bu hafta toplantı var"],
          "focus_sectors": ["Bankacilik", "Havacilik"],
          "avoid_sectors": ["Madencilik"],
          "timestamp": "..."
        }
    """
    if not _get("ANTHROPIC_API_KEY"):
        return _empty_plan("API anahtari eksik")

    if "error" in market_data:
        return _empty_plan(f"Piyasa verisi yok: {market_data['error']}")

    meta = market_data.get("_meta", {})
    regime = meta.get("regime", "unknown")
    benchmark_change = meta.get("benchmark_change", 0)
    bullish_count = meta.get("bullish_count", 0)
    total = meta.get("stage2_analyzed", 0)

    equity = portfolio.get("equity", 0) or 1000000
    cash = portfolio.get("cash", 0) or equity

    # Çoklu sinyal kaynağı context
    multi_context = ""
    try:
        from regime_detector import detect_regime
        quant = detect_regime(market_data)
        multi_context += f"\n**KANTİTATİF REJİM:** {quant['regime']} (skor {quant['quant_score']}/100, güven %{quant['confidence']})\n"
        multi_context += f"  Analiz: {quant['reasoning']}\n"
    except Exception:
        pass
    try:
        from anomaly_detector import detect_anomalies
        anomalies = detect_anomalies(market_data)
        if anomalies.get("anomaly_count", 0) > 0:
            multi_context += f"\n**ANOMALİLER:** {anomalies['risk_level'].upper()} risk, {anomalies['anomaly_count']} tespit\n"
            for a in anomalies.get("anomalies", [])[:3]:
                multi_context += f"  - {a['ticker']}: {a['detail']}\n"
    except Exception:
        pass
    try:
        from news_sentiment import get_market_sentiment
        from universe_loader import get_focus_list
        sent = get_market_sentiment(get_focus_list()[:10])
        if sent and not sent.get("error"):
            multi_context += f"\n**HABER SENTIMENT:** {sent.get('overall_sentiment', 'neutral')} (skor {sent.get('avg_score', 0):.2f})\n"
    except Exception:
        pass

    # Son 5 günlük performans
    perf_context = ""
    if last_trades:
        wins = sum(1 for t in last_trades[-10:] if (t.get("pnl_pct", 0) or 0) > 0)
        losses = sum(1 for t in last_trades[-10:] if (t.get("pnl_pct", 0) or 0) < 0)
        perf_context = f"\n**SON 10 İŞLEM:** {wins}W / {losses}L\n"

    prompt = f"""Sen bir AI Trading Risk Yöneticisisin. BIST (Borsa İstanbul) için günlük bir "briefing" hazırlıyorsun.
Kullanıcı (retail trader) Midas aracılığıyla elle işlem yapıyor — sen ona günün planını veriyorsun.

## MEVCUT DURUM
- Öz sermaye: ₺{equity:,.0f}
- Nakit: ₺{cash:,.0f} (%{(cash/equity*100):.0f})
- BIST 100 (XU100) bugün: {benchmark_change:+.2f}%
- Teknik rejim: {regime}
- Bugün güçlü alım sinyali: {bullish_count} / {total} hisse
{multi_context}
{perf_context}

## GÖREVİN
Bugünün planını belirle:

1. **day_quality**: Bugün nasıl bir gün?
   - "excellent" = güçlü rejim + bol sinyal + haberler pozitif → agresif
   - "good" = normal trend + seçici fırsat → ölçülü
   - "normal" = yatay piyasa → minimal işlem
   - "risky" = belirsizlik / yüksek volatilite → sadece watch
   - "skip" = kötü koşullar → **İŞLEM YOK**, nakitte kal

2. **daily_budget_try**: Bugün toplam kaç TL'yi işlemde tutarsın?
   - excellent: %25-30 portföy
   - good: %15-20
   - normal: %10
   - risky: %5 (sadece 1-2 test işlem)
   - skip: ₺0

3. **daily_profit_target_try**: Kâr hedefi?
   - excellent: %2-3 portföy
   - good: %1-1.5
   - normal: %0.5
   - risky/skip: ₺0 (hedef yok)

4. **max_trades**: Bugün max kaç işlem?
   - excellent: 8-10
   - good: 4-6
   - normal: 2-3
   - risky: 1-2
   - skip: 0

5. **credit_enabled**: Bugün kredili işlem önerebilir misin?
   - Sadece "excellent" gün + yüksek güven + kısa süreli kapanış ihtimali varsa true
   - Kredili önerdiysen mutlaka gün-içi kapatılabilir olmalı (Midas'ta faiz sonra işler)

6. **focus_sectors / avoid_sectors**: Hangi sektörler ön plan / kaçınılacak

## ÖNEMLİ İLKELER
- "Her gün kazanç" mecburiyeti YOK — kötü gün → SKIP
- Aç gözlü olma — max 10 işlem yeter
- Kredili işlem mutlak son çare, sadece çok güçlü sinyallerde
- Kullanıcı ANALYST modda — bu senin önerin, kullanıcı onaylayacak
- Teknik + haber + rejim + anomali ÇOKLU TEYİT şart

## YANIT FORMATI — SADECE JSON
{{
  "day_quality": "excellent | good | normal | risky | skip",
  "daily_budget_try": 250000,
  "daily_profit_target_try": 5000,
  "max_trades": 8,
  "credit_enabled": false,
  "reasoning": "2-3 cümle: bugün neden bu kalite, neden bu bütçe/hedef",
  "key_risks": ["Risk 1", "Risk 2"],
  "focus_sectors": ["Sektör 1", "Sektör 2"],
  "avoid_sectors": ["Sektör 3"],
  "market_outlook": "1 cümle özet — bugün genel hava",
  "confidence_in_plan": 8
}}

SADECE JSON döndür, başka metin YOK."""

    try:
        message = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}]
        )
        text = message.content[0].text.strip()
        result = _extract_json(text)
        result["timestamp"] = datetime.now(timezone.utc).isoformat()
        result["equity_at_briefing"] = equity
        result["benchmark_change"] = benchmark_change
        return result
    except Exception as e:
        return _empty_plan(f"Claude hatası: {str(e)[:150]}")


def _empty_plan(reason: str) -> dict:
    return {
        "day_quality": "unknown",
        "daily_budget_try": 0,
        "daily_profit_target_try": 0,
        "max_trades": 0,
        "credit_enabled": False,
        "reasoning": reason,
        "key_risks": [],
        "focus_sectors": [],
        "avoid_sectors": [],
        "market_outlook": reason,
        "confidence_in_plan": 0,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "error": reason,
    }
