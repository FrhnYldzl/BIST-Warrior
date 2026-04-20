# 🇹🇷 Meridian TR — Devam Etme Rehberi (HANDOFF)

**Proje konumu:** `C:\Users\FERHAN YILDIZLI\OneDrive\Desktop\meridian-tr\`
**Durum:** %40 tamamlandı — broker katmanı hazır, BIST adaptasyonları pending
**Tarih:** 2026-04-16

---

## 🎯 Proje Amacı

Meridian US (trading-agent) sistemini **ayrı bir proje** olarak BIST 100 için klonlamak.
- ✅ Aynı mimari (FastAPI + APScheduler + Claude + Gemini + SQLite)
- ✅ Broker: Interactive Brokers (IBKR paper) — kullanıcı hesabı açtı
- ✅ Fallback: yfinance paper simulator
- ⏳ Evren: BIST 100 (AKBNK, GARAN, THYAO vb.)
- ⏳ Dil: TR (dashboard, Claude promptları)
- ⏳ Para: TRY

---

## ✅ Tamamlananlar

| Dosya | Durum | Not |
|-------|-------|-----|
| `server/broker/ibkr.py` | ✅ YENİ | IBKR/ib_insync, BIST IBIS exchange, bracket order |
| `server/broker/equity.py` | ✅ YENİ | yfinance paper simulator (SQLite, 1M TRY başlangıç) |
| `server/broker/__init__.py` | ✅ YENİ | BROKER env var ile seçim (ibkr/yfinance) |
| `requirements.txt` | ✅ GÜNCEL | alpaca-py kaldırıldı, ib_insync + yfinance eklendi |
| `.env.example` | ✅ YENİ | IBKR + BIST ayarları |
| `Dockerfile`, `Procfile`, `railway.json` | ✅ KOPYA | Meridian US'den |
| 20+ server/*.py modülü | ✅ KOPYA | US versiyonu, henüz TR adaptasyonu yok |

---

## 🎯 Onaylı Mimari Kararı (2026-04-16)

**İki aşamalı tarama:**
- **Tier 1 (Core):** US = 15 hisse, TR = 20-25 BIST100 büyük — GARANTİLİ Claude'a gider
- **Tier 2 (Broad):** US = S&P500 + NASDAQ100 (~550), TR = BIST Yıldız+Ana (~400) — pre-filter → top 20
- **Tier 3 (Full):** NASDAQ Composite 3500 REDDEDİLDİ (micro-cap gürültü)
- **Feature flag:** `BROAD_SCAN_ENABLED` env var, default `false`, kontrollü açılır
- **Sonraki fazlar:** Options, Margin, Crypto (şimdilik ertelendi)

---

## ⏳ Pending (Yapılması Gerekenler)

### 0. Broad Scan Mimarisi — YENİ ÖNCELİK
- [ ] `server/market_scanner.py` → iki aşamalı yapı (pre-filter + Claude top 20)
- [ ] `server/universe.py` → S&P 500 + NASDAQ 100 listesi yükleyici (US için)
- [ ] `server/universe_bist.py` → BIST Yıldız + Ana Pazar listesi (TR için)
- [ ] `BROAD_SCAN_ENABLED` feature flag
- [ ] Meridian US'ye önce ekle (flag kapalı), sonra TR'ye kopyala

### 1. Config Adaptasyonu — KRİTİK
- [ ] `server/config.json` → BIST 100 watchlist yaz (AAPL → AKBNK.IS vb.)
- [ ] `server/config.py` → BENCHMARK="XU100", SECTOR_MAP TR sektörleri
- [ ] Sektör haritası TR: Bankacılık, Sanayi, Enerji, Perakende, Havacılık, Gıda

### 2. Scheduler & Saat Dilimi
- [ ] `server/scheduler.py` → TR saati (Europe/Istanbul)
- [ ] Market saatleri: Mon-Fri 10:00-18:00 TRT (=07:00-15:00 UTC)
- [ ] Pre-market/after-hours kaldırılmalı (BIST'te yok)

### 3. Claude Prompt Adaptasyonu
- [ ] `server/claude_brain.py` → prompt'u Türkçeleştir, BIST bağlamı ekle
- [ ] Fiyat formatı: ₺ TRY, lot büyüklüğü farklı olabilir
- [ ] Kısa satış yasağı prompt'a yaz ("BIST'te short yok")

### 4. Dashboard TR
- [ ] `server/static/index.html` → 🇹🇷 bayrak, "Meridian TR" başlık
- [ ] TRY simgesi (₺), Türkçe etiketler
- [ ] BIST 100 grafiği (SPY yerine XU100)

### 5. Diğer Modüller
- [ ] `main.py` → SPY → XU100 referansları
- [ ] `notifier.py` → TR saat/para birimi
- [ ] `market_scanner.py` → .IS suffix veya IBKR symbol mapping
- [ ] `regime_detector.py` → XU100 benchmark
- [ ] `news_sentiment.py` → TR finansal haberler (bloomberght, foreks vb.)

### 6. IB Gateway Kurulum
- [ ] Kullanıcı IB Gateway Paper indirecek: https://www.interactivebrokers.com/en/trading/ib-gateway-download.php
- [ ] Port 7497 açık, API Settings → Enable ActiveX and Socket Clients ✅
- [ ] Read-Only API kapalı olmalı (emir gönderebilmek için)
- [ ] Trusted IPs: 127.0.0.1

### 7. Deployment Kararı
- [ ] IBKR ile Railway sorunlu (Gateway headless zor) → **Seçenekler:**
  - A) Yerel PC'de çalıştır (IB Gateway açık, 7/24 ev bilgisayarı)
  - B) VPS (Hetzner/DigitalOcean) kirala + Gateway kur + systemd
  - C) Önce yfinance paper ile Railway'de test, sonra IBKR'ye geç

---

## 🔁 Yeni Konuşmaya Nasıl Devam Ederim?

**Bu metni yeni sohbette kopyala:**

> Selam! Meridian TR projesi üzerinde çalışıyoruz. Konum:
> `C:\Users\FERHAN YILDIZLI\OneDrive\Desktop\meridian-tr\`
>
> Durum dosyasını oku: `HANDOFF.md`
>
> Şu adımdan devam edelim: **[hangi maddeden devam edeceksen yaz — örn. "Config adaptasyonu 1. madde"]**
>
> IBKR paper hesabım açık, IB Gateway [kurdum/kurmadım].

Bu kadar — yeni Claude tüm durum dosyasını okuyacak ve kaldığın yerden devam edecek.

---

## 📂 Meridian US'den Farklar (Özet)

| Konu | US (Meridian) | TR (Meridian-TR) |
|------|--------------|------------------|
| Broker | Alpaca paper | IBKR paper (+ yfinance fallback) |
| Evren | 15 US stock | ~30 BIST 100 stock |
| Para | USD | TRY |
| Saat | 09:30-16:00 EST | 10:00-18:00 TRT |
| Benchmark | SPY | XU100 (BIST 100) |
| Dil | EN | TR |
| Short | ✅ Var | ❌ Blokla |
| PDT | ✅ Var | ❌ Yok |

---

## 🔧 Yerel Test Komutu (kurulum sonrası)

```bash
cd meridian-tr
pip install -r requirements.txt
cp .env.example .env
# .env'yi düzenle: ANTHROPIC_API_KEY, IBKR_PORT
# IB Gateway'i başlat (paper, port 7497)
uvicorn server.main:app --port 8000
```

---

## ⚠️ Bilinen Riskler

1. **IBKR + Railway uyumsuz** — Gateway'in GUI'si var, cloud'da zor çalışır
2. **BIST market data** — IBKR için ek abonelik gerekebilir (~$5/ay)
3. **yfinance BIST gecikmesi** — 15-20dk gecikmeli, HFT uygun değil
4. **Meridian US dokunulmamış** — Bu proje onu etkilemiyor, paralel çalışır

---

**Devir-teslim tamam. Çıkabilirsin, her şey kayıtlı.** 👋
