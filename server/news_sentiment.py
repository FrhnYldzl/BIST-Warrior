"""
news_sentiment.py — Haber Sentiment Analizi (V3.3)

Alpaca News API'dan son haberleri çeker, basit NLP ile sentiment skoru hesaplar.
Claude AI'a haber context'i sağlar.

Özellikler:
  - Ticker bazlı haber çekme
  - Keyword bazlı sentiment skorlama (bullish/bearish kelime analizi)
  - Haber yoğunluğu takibi (anormal haber sayısı = dikkat)
  - Son 24 saat özeti
"""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv, dotenv_values

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)
_env_vals = dotenv_values(_env_path)

def _get(key): return os.getenv(key) or _env_vals.get(key, "")


# ─── Sentiment Kelime Sözlüğü ─────────���──────────────────────────

BULLISH_WORDS = {
    # Güçlü bullish
    "surge": 3, "soar": 3, "skyrocket": 3, "breakout": 3, "record high": 3,
    "all-time high": 3, "beat expectations": 3, "blowout": 3, "upgrade": 3,
    # Orta bullish
    "rally": 2, "gain": 2, "rise": 2, "jump": 2, "boost": 2,
    "growth": 2, "outperform": 2, "bullish": 2, "upbeat": 2,
    "strong earnings": 2, "beat": 2, "exceeded": 2, "positive": 2,
    # Hafif bullish
    "up": 1, "higher": 1, "improve": 1, "recover": 1, "rebound": 1,
    "buy": 1, "accumulate": 1, "opportunity": 1, "optimistic": 1,
}

BEARISH_WORDS = {
    # Güçlü bearish
    "crash": -3, "plunge": -3, "collapse": -3, "bankruptcy": -3,
    "fraud": -3, "investigation": -3, "downgrade": -3, "miss": -3,
    "recession": -3, "crisis": -3,
    # Orta bearish
    "sell-off": -2, "decline": -2, "drop": -2, "fall": -2, "loss": -2,
    "bearish": -2, "warning": -2, "weak": -2, "concern": -2,
    "layoff": -2, "cut": -2, "disappointing": -2, "below expectations": -2,
    # Hafif bearish
    "down": -1, "lower": -1, "risk": -1, "uncertainty": -1,
    "volatile": -1, "sell": -1, "cautious": -1, "pressure": -1,
}


def _score_text(text: str) -> int:
    """Metin sentiment skoru (-10 to +10 arası)."""
    text_lower = text.lower()
    score = 0
    for word, val in BULLISH_WORDS.items():
        if word in text_lower:
            score += val
    for word, val in BEARISH_WORDS.items():
        if word in text_lower:
            score += val  # Already negative
    return max(-10, min(10, score))


# ─── Alpaca News API ──────────────────────────────────────────────

def get_news(tickers: list[str] = None, limit: int = 20) -> list[dict]:
    """
    Alpaca News API'dan haber çek.
    Returns: [{headline, summary, source, created_at, symbols, sentiment_score}]
    """
    try:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.data.requests import NewsRequest

        client = StockHistoricalDataClient(
            api_key=_get("ALPACA_API_KEY"),
            secret_key=_get("ALPACA_SECRET_KEY"),
        )

        params = {"limit": limit}
        if tickers:
            params["symbols"] = tickers

        # Alpaca News API
        import requests as req_lib
        base_url = "https://data.alpaca.markets/v1beta1/news"
        headers = {
            "APCA-API-KEY-ID": _get("ALPACA_API_KEY"),
            "APCA-API-SECRET-KEY": _get("ALPACA_SECRET_KEY"),
        }
        query = {"limit": limit, "sort": "desc"}
        if tickers:
            query["symbols"] = ",".join(tickers)

        resp = req_lib.get(base_url, headers=headers, params=query, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        articles = []
        for item in data.get("news", []):
            headline = item.get("headline", "")
            summary = item.get("summary", "")
            full_text = f"{headline} {summary}"
            sentiment = _score_text(full_text)

            articles.append({
                "headline": headline,
                "summary": summary[:200] if summary else "",
                "source": item.get("source", "unknown"),
                "created_at": item.get("created_at", ""),
                "symbols": item.get("symbols", []),
                "url": item.get("url", ""),
                "sentiment_score": sentiment,
            })

        return articles

    except Exception as e:
        return [{"error": str(e)}]


def get_ticker_sentiment(ticker: str, limit: int = 10) -> dict:
    """
    Tek ticker için haber sentiment özeti.
    Returns: {ticker, articles, avg_sentiment, sentiment_label, news_intensity, summary}
    """
    articles = get_news(tickers=[ticker], limit=limit)
    if articles and "error" in articles[0]:
        return {"ticker": ticker, "error": articles[0]["error"]}

    if not articles:
        return {
            "ticker": ticker,
            "articles": [],
            "avg_sentiment": 0,
            "sentiment_label": "no_news",
            "news_intensity": "low",
            "summary": f"{ticker} için son haber bulunamadı",
        }

    scores = [a["sentiment_score"] for a in articles]
    avg = sum(scores) / len(scores)

    # Sentiment label
    if avg >= 3:
        label = "very_bullish"
    elif avg >= 1:
        label = "bullish"
    elif avg >= -1:
        label = "neutral"
    elif avg >= -3:
        label = "bearish"
    else:
        label = "very_bearish"

    # Haber yoğunluğu (saatte kaç haber?)
    now = datetime.now(timezone.utc)
    recent_count = 0
    for a in articles:
        try:
            created = datetime.fromisoformat(a["created_at"].replace("Z", "+00:00"))
            if (now - created).total_seconds() < 3600:  # Son 1 saat
                recent_count += 1
        except Exception:
            pass

    if recent_count >= 5:
        intensity = "very_high"
    elif recent_count >= 3:
        intensity = "high"
    elif recent_count >= 1:
        intensity = "normal"
    else:
        intensity = "low"

    return {
        "ticker": ticker,
        "article_count": len(articles),
        "articles": articles[:5],  # İlk 5 haber
        "avg_sentiment": round(avg, 2),
        "sentiment_label": label,
        "news_intensity": intensity,
        "summary": _generate_summary(ticker, articles, avg, label, intensity),
    }


def get_market_sentiment(tickers: list[str]) -> dict:
    """
    Tüm watchlist için toplu sentiment analizi.
    Returns: {overall_sentiment, per_ticker, notable_headlines}
    """
    results = {}
    all_scores = []
    notable = []

    for ticker in tickers:
        try:
            r = get_ticker_sentiment(ticker, limit=5)
            results[ticker] = {
                "avg_sentiment": r.get("avg_sentiment", 0),
                "label": r.get("sentiment_label", "neutral"),
                "intensity": r.get("news_intensity", "low"),
                "article_count": r.get("article_count", 0),
            }
            all_scores.append(r.get("avg_sentiment", 0))

            # Öne çıkan haberler (yüksek sentiment skoru)
            for a in r.get("articles", []):
                if abs(a.get("sentiment_score", 0)) >= 3:
                    notable.append({
                        "ticker": ticker,
                        "headline": a["headline"],
                        "sentiment": a["sentiment_score"],
                        "source": a.get("source", ""),
                    })
        except Exception:
            results[ticker] = {"avg_sentiment": 0, "label": "error", "intensity": "unknown"}

    overall_avg = sum(all_scores) / len(all_scores) if all_scores else 0
    if overall_avg >= 2:
        overall = "bullish"
    elif overall_avg >= 0.5:
        overall = "slightly_bullish"
    elif overall_avg >= -0.5:
        overall = "neutral"
    elif overall_avg >= -2:
        overall = "slightly_bearish"
    else:
        overall = "bearish"

    # En dikkat çekici haberleri sırala
    notable.sort(key=lambda x: abs(x["sentiment"]), reverse=True)

    return {
        "overall_sentiment": overall,
        "overall_score": round(overall_avg, 2),
        "per_ticker": results,
        "notable_headlines": notable[:10],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _generate_summary(ticker: str, articles: list, avg: float, label: str, intensity: str) -> str:
    """Ticker sentiment özet metni."""
    parts = [f"{ticker}: {label} sentiment (skor: {avg:.1f})"]

    if intensity in ("high", "very_high"):
        parts.append(f"DIKKAT: Yüksek haber yoğunluğu ({intensity})")

    if articles:
        top = max(articles, key=lambda a: abs(a.get("sentiment_score", 0)))
        parts.append(f"En etkili: \"{top['headline'][:80]}\" ({top['source']})")

    return " | ".join(parts)
