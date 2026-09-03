from __future__ import annotations

from django.core.cache import cache

RATE_LIMIT_WINDOW_SECONDS = 300  # 5 دقیقه
RATE_LIMIT_MAX_ATTEMPTS = 3


def is_rate_limited(key: str) -> bool:
    cache_key = f"rate_limit:{key}"
    attempts = cache.get(cache_key, 0)
    return attempts >= RATE_LIMIT_MAX_ATTEMPTS


def record_attempt(key: str) -> None:
    cache_key = f"rate_limit:{key}"
    attempts = cache.get(cache_key, 0)
    cache.set(cache_key, attempts + 1, timeout=RATE_LIMIT_WINDOW_SECONDS)
