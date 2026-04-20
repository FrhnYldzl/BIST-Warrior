"""
config.py — Merkezi Konfigürasyon Motoru (V3 — TR adaptasyonu)

Tüm parametreler tek merkezden yönetilir.
Öncelik sırası: Environment Variable > config.json > varsayılan değer
"""

import json
import os
from pathlib import Path


_CONFIG_PATH = Path(__file__).parent / "config.json"
_file_config: dict = {}

if _CONFIG_PATH.exists():
    with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
        _file_config = json.load(f)


def _get(key: str, default=None, cast=None):
    """Env > config.json > default sırasıyla değer al."""
    val = os.getenv(key) or _file_config.get(key, default)
    if val is None:
        return default
    if cast:
        try:
            return cast(val)
        except (ValueError, TypeError):
            return default
    return val


# ═══════════════════════════════════════════════════════════════
# GENEL
# ═══════════════════════════════════════════════════════════════

PORT = _get("PORT", 8000, int)
WEBHOOK_SECRET = _get("WEBHOOK_SECRET", "")
AI_MODEL = _get("AI_MODEL", "claude-sonnet-4-6")

# ═══════════════════════════════════════════════════════════════
# DATA DIR — Railway Volume desteği
# ═══════════════════════════════════════════════════════════════
# Railway'de /data Volume mount edilir, lokal'de server/ dizini kullanılır
# Bu dizin SQLite dosyalarının (midas_journal.db, trades.db, paper_bist.db) yaşayacağı yerdir
import os as _os
DATA_DIR = _get("MIDAS_DATA_DIR", None)
if not DATA_DIR:
    DATA_DIR = str(Path(__file__).parent)   # Lokal: server/
# Railway volume varsa onu kullan
if _os.path.exists("/data") and _os.access("/data", _os.W_OK):
    DATA_DIR = "/data"
# Dizini garanti altına al
try:
    _os.makedirs(DATA_DIR, exist_ok=True)
except Exception:
    pass

# ═══════════════════════════════════════════════════════════════
# ÇALIŞMA MODU (Meridian Capital Türkiye — Midas Destekli)
# ═══════════════════════════════════════════════════════════════
# "analyst" = AI sinyal üretir, kullanıcı Midas'tan elle emir girer (ÖNERİLEN)
# "auto"    = Claude kararları broker'a direkt gider
OPERATION_MODE = _get("OPERATION_MODE", "analyst").lower()
AUTO_EXECUTE = _get("AUTO_EXECUTE", "false").lower() in ("true", "1", "yes") \
    if isinstance(_get("AUTO_EXECUTE", False), str) else bool(_get("AUTO_EXECUTE", False))

# Bildirim ayarları
HIGH_CONFIDENCE_ALERT = _get("HIGH_CONFIDENCE_ALERT", "true").lower() in ("true", "1", "yes") \
    if isinstance(_get("HIGH_CONFIDENCE_ALERT", True), str) else bool(_get("HIGH_CONFIDENCE_ALERT", True))
MORNING_BRIEF_HOUR = _get("MORNING_BRIEF_HOUR", 9, int)
MORNING_BRIEF_MINUTE = _get("MORNING_BRIEF_MINUTE", 45, int)

# ═══════════════════════════════════════════════════════════════
# PİYASA LOKALİZASYONU (BIST)
# ═══════════════════════════════════════════════════════════════

CURRENCY = _get("CURRENCY", "TRY")
CURRENCY_SYMBOL = _get("CURRENCY_SYMBOL", "₺")
MARKET_TZ = _get("MARKET_TZ", "Europe/Istanbul")
MARKET_OPEN = _get("MARKET_OPEN", "10:00")          # TRT
MARKET_CLOSE = _get("MARKET_CLOSE", "18:00")        # TRT
SHORT_ENABLED = _get("SHORT_ENABLED", "false").lower() in ("true", "1", "yes") \
    if isinstance(_get("SHORT_ENABLED", False), str) else bool(_get("SHORT_ENABLED", False))

# ═══════════════════════════════════════════════════════════════
# RİSK YÖNETİMİ
# ═══════════════════════════════════════════════════════════════

MAX_RISK_PCT = _get("MAX_RISK_PCT", 0.02, float)
MAX_POSITION_PCT = _get("MAX_POSITION_PCT", 0.15, float)         # Tek pozisyon max portföy %'si
MAX_SECTOR_PCT = _get("MAX_SECTOR_PCT", 0.50, float)             # Tek sektör max portföy %'si (BIST'te bankalar ağır)
ATR_MULTIPLIER = _get("ATR_MULTIPLIER", 1.5, float)              # ATR stop-loss çarpanı
ORDER_COOLDOWN_SEC = _get("ORDER_COOLDOWN_SEC", 60, int)         # Aynı ticker emir bekleme süresi
FLASH_CRASH_THRESHOLD = _get("FLASH_CRASH_THRESHOLD", 0.05, float)  # %5 anlık düşüş = failsafe

# Güven skoru → risk yüzdesi haritası
CONFIDENCE_RISK_MAP = _file_config.get("CONFIDENCE_RISK_MAP", {
    "10": 0.020, "9": 0.020, "8": 0.018, "7": 0.015,
    "6": 0.012, "5": 0.010, "4": 0.008,
    "3": 0.000, "2": 0.000, "1": 0.000,
})

# Rejim → risk çarpanı haritası
REGIME_MULTIPLIERS = _file_config.get("REGIME_MULTIPLIERS", {
    "bull_strong": 1.0, "bull": 0.9, "neutral": 0.7,
    "bear": 0.5, "bear_strong": 0.3,
})

# Rejim → max yatırım yüzdesi
REGIME_MAX_INVESTED = _file_config.get("REGIME_MAX_INVESTED", {
    "bull_strong": 95, "bull": 85, "neutral": 70,
    "bear": 40, "bear_strong": 30,
})

# ═══════════════════════════════════════════════════════════════
# PİYASA TARAMA (BIST 100)
# ═══════════════════════════════════════════════════════════════

WATCHLIST = _file_config.get("WATCHLIST", [
    # Bankacilik
    "AKBNK.IS", "GARAN.IS", "ISCTR.IS", "YKBNK.IS", "HALKB.IS", "VAKBN.IS",
    # Holding
    "KCHOL.IS", "SAHOL.IS",
    # Savunma / Sanayi
    "ASELS.IS", "TUPRS.IS", "EREGL.IS", "KRDMD.IS",
    # Havacilik
    "THYAO.IS", "PGSUS.IS", "TAVHL.IS",
    # Perakende / Gida
    "BIMAS.IS", "MGROS.IS", "ULKER.IS",
    # Telekom
    "TCELL.IS", "TTKOM.IS",
    # Otomotiv / Beyaz-Esya
    "TOASO.IS", "FROTO.IS", "ARCLK.IS", "VESTL.IS",
])

BENCHMARK = _get("BENCHMARK", "XU100.IS")               # BIST 100 endeksi (yfinance)
SCAN_INTERVAL_MIN = _get("SCAN_INTERVAL_MIN", 10, int)
LOOKBACK_DAYS = _get("LOOKBACK_DAYS", 90, int)

# Momentum sinyal eşikleri
SIGNAL_GAP_THRESHOLD = _get("SIGNAL_GAP_THRESHOLD", 4.0, float)
SIGNAL_VOLUME_THRESHOLD = _get("SIGNAL_VOLUME_THRESHOLD", 2.0, float)

# ═══════════════════════════════════════════════════════════════
# İKİ AŞAMALI TARAMA (Wide-scan → Narrow-focus)
# ═══════════════════════════════════════════════════════════════

# "watchlist"  — sadece Focus List (küçük, hızlı)
# "full"       — sadece geniş evren (WATCHLIST filtreden geçmezse dahil değil)
# "hybrid"     — Focus List her zaman dahil + evrenden pre-filter ile top N eklenir (önerilen)
SCAN_UNIVERSE = _get("SCAN_UNIVERSE", "hybrid")

# Evre 1 → Evre 2 geçişi için eşikler
PREFILTER_TOP_N = _get("PREFILTER_TOP_N", 25, int)                   # Focus List'e eklenecek max aday
PREFILTER_MIN_VOLUME_RATIO = _get("PREFILTER_MIN_VOLUME_RATIO", 1.5, float)   # Hacim patlaması
PREFILTER_MIN_MOMENTUM_SCORE = _get("PREFILTER_MIN_MOMENTUM_SCORE", 60, int)  # Basit momentum eşiği
PREFILTER_MIN_ABS_CHANGE = _get("PREFILTER_MIN_ABS_CHANGE", 2.0, float)       # |günlük değişim %|

# Likidite tabanı — altında kalan hisse Evre 1'de düşer
MIN_LIQUIDITY_TRY = _get("MIN_LIQUIDITY_TRY", 1_000_000, float)       # Ortalama günlük hacim (TRY)

# ═══════════════════════════════════════════════════════════════
# SEKTÖR HARİTASI (BIST — TR sektör adları)
# ═══════════════════════════════════════════════════════════════

SECTOR_MAP = _file_config.get("SECTOR_MAP", {
    "AKBNK.IS": "Bankacilik", "GARAN.IS": "Bankacilik", "ISCTR.IS": "Bankacilik",
    "YKBNK.IS": "Bankacilik", "HALKB.IS": "Bankacilik", "VAKBN.IS": "Bankacilik",
    "KCHOL.IS": "Holding", "SAHOL.IS": "Holding",
    "ASELS.IS": "Savunma",
    "TUPRS.IS": "Enerji", "EREGL.IS": "Demir-Celik", "KRDMD.IS": "Demir-Celik",
    "THYAO.IS": "Havacilik", "PGSUS.IS": "Havacilik", "TAVHL.IS": "Havacilik",
    "BIMAS.IS": "Perakende", "MGROS.IS": "Perakende", "ULKER.IS": "Gida",
    "TCELL.IS": "Telekom", "TTKOM.IS": "Telekom",
    "TOASO.IS": "Otomotiv", "FROTO.IS": "Otomotiv",
    "ARCLK.IS": "Beyaz-Esya", "VESTL.IS": "Beyaz-Esya",
})

# ═══════════════════════════════════════════════════════════════
# BRACKET ORDER AYARLARI
# ═══════════════════════════════════════════════════════════════

BRACKET_ENABLED = _get("BRACKET_ENABLED", True, bool)
DEFAULT_RR_RATIO = _get("DEFAULT_RR_RATIO", 2.0, float)          # Risk/Reward oranı

# ═══════════════════════════════════════════════════════════════
# AI APPROVAL
# ═══════════════════════════════════════════════════════════════

AI_APPROVAL_REQUIRED = _get("AI_APPROVAL_REQUIRED", "false").lower() in ("true", "1", "yes") \
    if isinstance(_get("AI_APPROVAL_REQUIRED", False), str) else bool(_get("AI_APPROVAL_REQUIRED", False))

# ═══════════════════════════════════════════════════════════════
# GEMINI COUNCIL (V4.5)
# ═══════════════════════════════════════════════════════════════

GEMINI_API_KEY = _get("GEMINI_API_KEY", "")
GEMINI_MODEL = _get("GEMINI_MODEL", "gemini-2.0-flash")
COUNCIL_ENABLED = _get("COUNCIL_ENABLED", "true").lower() in ("true", "1", "yes") \
    if isinstance(_get("COUNCIL_ENABLED", True), str) else bool(_get("COUNCIL_ENABLED", True))

# ═══════════════════════════════════════════════════════════════
# BROKER (IBKR / yfinance)
# ═══════════════════════════════════════════════════════════════

BROKER = _get("BROKER", "yfinance")                              # "ibkr" | "yfinance"
IBKR_HOST = _get("IBKR_HOST", "127.0.0.1")
IBKR_PORT = _get("IBKR_PORT", 7497, int)                         # 7497 paper, 7496 live
IBKR_CLIENT_ID = _get("IBKR_CLIENT_ID", 1, int)
IBKR_EXCHANGE = _get("IBKR_EXCHANGE", "IBIS")                    # BIST IBKR kodu


def get_all() -> dict:
    """Tüm konfigürasyonu döndür (dashboard için)."""
    return {
        "currency": CURRENCY,
        "currency_symbol": CURRENCY_SYMBOL,
        "market_tz": MARKET_TZ,
        "market_open": MARKET_OPEN,
        "market_close": MARKET_CLOSE,
        "short_enabled": SHORT_ENABLED,
        "max_risk_pct": MAX_RISK_PCT,
        "max_position_pct": MAX_POSITION_PCT,
        "max_sector_pct": MAX_SECTOR_PCT,
        "atr_multiplier": ATR_MULTIPLIER,
        "order_cooldown_sec": ORDER_COOLDOWN_SEC,
        "flash_crash_threshold": FLASH_CRASH_THRESHOLD,
        "watchlist": WATCHLIST,
        "benchmark": BENCHMARK,
        "scan_interval_min": SCAN_INTERVAL_MIN,
        "ai_model": AI_MODEL,
        "bracket_enabled": BRACKET_ENABLED,
        "default_rr_ratio": DEFAULT_RR_RATIO,
        "ai_approval_required": AI_APPROVAL_REQUIRED,
        "regime_multipliers": REGIME_MULTIPLIERS,
        "regime_max_invested": REGIME_MAX_INVESTED,
        "sector_map": SECTOR_MAP,
        "gemini_model": GEMINI_MODEL,
        "council_enabled": COUNCIL_ENABLED,
        "broker": BROKER,
    }
