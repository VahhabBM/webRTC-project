import base64
import hashlib
import hmac
import time

from django.conf import settings

DEFAULT_STUN_SERVERS = [
    "stun:stun.l.google.com:19302",
    "stun:stun1.l.google.com:19302",
]

DEFAULT_TTL_SECONDS = 3600  # ۱ ساعت


def generate_ice_servers(
    participant_id: str,
    ttl: int = DEFAULT_TTL_SECONDS,
    turn_secret: str | None = None,
    turn_urls: list[str] | None = None,
    stun_urls: list[str] | None = None,
) -> dict:
    """تولید کانفیگ استاندارد RTCIceServer منطبق با RFC 5766

    دارای مکانیزم STUN Fallback در صورت نبود تنظیمات TURN.
    """
    secret = turn_secret or getattr(settings, "TURN_SHARED_SECRET", "")
    configured_turn_urls = turn_urls or getattr(settings, "TURN_URLS", [])
    configured_stun_urls = stun_urls or getattr(
        settings, "STUN_URLS", DEFAULT_STUN_SERVERS
    )

    ice_servers = [{"urls": configured_stun_urls}]

    # اگر TURN سکرت و آدرس‌ها ست شده باشند، کریدنشال موقت HMAC-SHA1 می‌سازیم
    if secret and configured_turn_urls:
        expiry = int(time.time()) + int(ttl)
        username = f"{expiry}:{participant_id}"

        # HMAC-SHA1 Signature
        digest = hmac.new(
            secret.encode("utf-8"), username.encode("utf-8"), hashlib.sha1
        ).digest()
        credential = base64.b64encode(digest).decode("utf-8")

        ice_servers.append(
            {
                "urls": configured_turn_urls,
                "username": username,
                "credential": credential,
            }
        )

        return {
            "ice_servers": ice_servers,
            "ttl": ttl,
            "expires_at": expiry,
            "fallback_only": False,
        }

    # در نبود Secret یا سرور TURN، فال‌بک امن به STUN داده می‌شود
    return {
        "ice_servers": ice_servers,
        "ttl": ttl,
        "expires_at": None,
        "fallback_only": True,
    }
