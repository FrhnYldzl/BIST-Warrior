"""
auth.py — Tek kullanıcı (admin) basit auth (Sprint 2)

Tasarım kararı:
  • BIST Warrior tek kullanıcılı (kullanıcının kendi trader'ı)
  • Multi-user şu an gerekmiyor → minimum dependency, basit kod
  • AUTH_PASSWORD env'i set edilmemişse auth DEVRE DIŞI (geliştirme/local)
  • Set edilmişse imzalı session cookie ile koruma + login form

Mekanizma:
  • Login: POST /auth/login JSON {password} → cookie set
  • Cookie: base64(timestamp|hmac_sha256(secret, timestamp+ua))
  • Süre: 7 gün, hover'da yenilenir
  • Logout: POST /auth/logout → cookie sil
  • Middleware tüm /api/* + / için cookie kontrol eder, /auth/* hariç
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import os
import secrets
import time
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

_env_path = Path(__file__).parent.parent / ".env"
load_dotenv(_env_path)
_env_vals = dotenv_values(_env_path)

def _get(key: str, default: str = "") -> str:
    return os.getenv(key) or _env_vals.get(key, "") or default


# ──────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────

COOKIE_NAME = "bw_session"
COOKIE_TTL = 7 * 24 * 3600  # 7 gün
PASSWORD = _get("AUTH_PASSWORD", "")  # Boşsa auth devre dışı

# Cookie imzası için secret. Yoksa rastgele üret (her boot'ta cookie'ler invalid olur — güvenli default).
SECRET = _get("AUTH_SECRET", "") or secrets.token_hex(32)


def is_enabled() -> bool:
    """AUTH_PASSWORD set edilmişse auth aktif."""
    return bool(PASSWORD)


# ──────────────────────────────────────────────────────────────────
# Password verify
# ──────────────────────────────────────────────────────────────────

def verify_password(plain: str) -> bool:
    if not PASSWORD or not plain:
        return False
    return hmac.compare_digest(plain.encode(), PASSWORD.encode())


# ──────────────────────────────────────────────────────────────────
# Cookie issue/verify
# ──────────────────────────────────────────────────────────────────

def _sign(payload: str) -> str:
    return hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()


def issue_token() -> str:
    """timestamp:nonce.signature — base64-safe."""
    ts = str(int(time.time()))
    nonce = secrets.token_hex(8)
    payload = f"{ts}:{nonce}"
    sig = _sign(payload)
    raw = f"{payload}.{sig}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def verify_token(token: str | None) -> bool:
    if not token:
        return False
    try:
        # Padding'i geri ekle
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode()
        if "." not in raw:
            return False
        payload, sig = raw.rsplit(".", 1)
        if not hmac.compare_digest(_sign(payload), sig):
            return False
        ts_str, _ = payload.split(":", 1)
        ts = int(ts_str)
        if time.time() - ts > COOKIE_TTL:
            return False
        return True
    except Exception:
        return False


# ──────────────────────────────────────────────────────────────────
# Path bypass list (login/logout/static login form)
# ──────────────────────────────────────────────────────────────────

PUBLIC_PREFIXES: tuple[str, ...] = (
    "/auth/",
    "/static/login",
    "/login.html",
    "/favicon",
    "/api/health",  # uptime ping için açık tut
)


def is_public(path: str) -> bool:
    return any(path.startswith(p) for p in PUBLIC_PREFIXES)
