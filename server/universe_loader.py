"""
universe_loader.py — BIST Evren Yükleyici (Meridian TR V1)

İki aşamalı tarama için geniş BIST evrenini yönetir:
  - Statik fallback: bist_universe.json (~380 ticker)
  - Dinamik refresh: isteğe bağlı, yfinance/KAP/3rd-party kaynaklardan güncelleme
  - Evrenin tamamını WATCHLIST (Focus List) ile birleştirir

Kullanim:
    from universe_loader import get_universe, get_sector_map

    tickers = get_universe()          # List[str] — .IS'li tum hisseler
    sectors = get_sector_map()        # Dict[ticker, sector]
"""

import json
import os
from pathlib import Path
from typing import Optional

_UNIVERSE_PATH = Path(__file__).parent / "bist_universe.json"

# ─── Import guard: config.py henuz tam yuklenmemis olabilir (cyclic) ──
try:
    from config import WATCHLIST as _FOCUS_LIST, SECTOR_MAP as _FOCUS_SECTORS
except Exception:
    _FOCUS_LIST = []
    _FOCUS_SECTORS = {}


_cache: dict = {}  # {"tickers": [...], "sector_map": {...}, "version": "..."}


def _load_from_disk() -> dict:
    """bist_universe.json'i oku ve cache'le."""
    global _cache
    if _cache:
        return _cache

    if not _UNIVERSE_PATH.exists():
        print(f"[universe] UYARI: {_UNIVERSE_PATH.name} bulunamadi, sadece WATCHLIST kullanilacak.")
        _cache = {
            "tickers": list(_FOCUS_LIST),
            "sector_map": dict(_FOCUS_SECTORS),
            "version": "fallback-empty",
        }
        return _cache

    try:
        with open(_UNIVERSE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        sector_map = data.get("sector_map", {})
        # Tickers listesi sector_map anahtarlarindan türetilir
        tickers = sorted(set(sector_map.keys()) | set(_FOCUS_LIST))
        # Focus list sektörleri universe'u override eder (kullanici netlestirdi)
        merged_sectors = {**sector_map, **_FOCUS_SECTORS}
        _cache = {
            "tickers": tickers,
            "sector_map": merged_sectors,
            "version": data.get("version", "unknown"),
            "source": data.get("source", "static"),
            "count": len(tickers),
        }
        return _cache
    except Exception as e:
        print(f"[universe] HATA okuma: {e}")
        _cache = {
            "tickers": list(_FOCUS_LIST),
            "sector_map": dict(_FOCUS_SECTORS),
            "version": "error-fallback",
        }
        return _cache


def get_universe() -> list:
    """Tum BIST hisselerinin .IS'li listesi (Focus + geniş evren, tekilleştirilmiş)."""
    return _load_from_disk()["tickers"]


def get_sector_map() -> dict:
    """Ticker → TR sektor adi (Focus List sektörleri universe'u override eder)."""
    return _load_from_disk()["sector_map"]


def get_focus_list() -> list:
    """Sadece config.json WATCHLIST (Focus List) — Claude'a her zaman garanti gönderilecek."""
    return list(_FOCUS_LIST)


def get_universe_info() -> dict:
    """Dashboard ve debug için özet bilgi."""
    data = _load_from_disk()
    return {
        "version": data.get("version"),
        "source": data.get("source"),
        "count": data.get("count", len(data.get("tickers", []))),
        "focus_list_count": len(_FOCUS_LIST),
        "sectors": sorted(set(data.get("sector_map", {}).values())),
    }


def refresh_universe(source: str = "yfinance") -> dict:
    """
    Dinamik güncelleme placeholder'i.

    Su an sadece disk cache'ini sifirliyor. İleride:
      - yfinance ile BIST exchange listesi
      - KAP (Kamuyu Aydınlatma Platformu) scrape
      - 3rd-party data provider
    entegrasyonlari eklenebilir.

    source:
        "yfinance"  — (TODO) yfinance'ten exchange=BIST listele
        "disk"       — sadece cache invalidate
    """
    global _cache
    _cache = {}
    # TODO: dinamik kaynak entegrasyonu
    # if source == "yfinance":
    #     ...BIST listesi cek, bist_universe.json'i guncelle...
    return {
        "refreshed": True,
        "source": source,
        "note": "Statik dosya yeniden yuklendi. Dinamik kaynak entegrasyonu pending.",
        **get_universe_info(),
    }


if __name__ == "__main__":
    info = get_universe_info()
    print(f"BIST Evreni: {info['count']} hisse, {len(info['sectors'])} sektör")
    print(f"Versiyon: {info['version']}, kaynak: {info['source']}")
    print(f"Focus List: {info['focus_list_count']} hisse")
