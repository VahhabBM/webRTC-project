"""STUN/TURN temporary credential generation service (T-28, T-35 & T-36)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


class TurnCredentialService:
    """تولید اطلاعات اتصال موقت به سرورهای رله چندمنطقه‌ای (Region 1 & Region 2 Coturn)."""

    def __init__(self, ttl: int | None = None):
        self.domain = getattr(
            settings, "COTURN_REGION1_DOMAIN", "staging-turn.yourdomain.com"
        )
        self.port = getattr(settings, "COTURN_PORT", 3478)
        self.tls_port = getattr(settings, "COTURN_TLS_PORT", 5349)

        # Region 2 (T-36 Multi-Region Failover)
        self.region2_domain = getattr(settings, "COTURN_REGION2_DOMAIN", None)
        self.region2_port = getattr(settings, "COTURN_REGION2_PORT", self.port)
        self.region2_tls_port = getattr(
            settings, "COTURN_REGION2_TLS_PORT", self.tls_port
        )

        self.shared_secret = getattr(settings, "COTURN_SHARED_SECRET", "") or getattr(
            settings, "TURN_SHARED_SECRET", ""
        )
        self.region2_shared_secret = (
            getattr(settings, "COTURN_REGION2_SHARED_SECRET", "") or self.shared_secret
        )

        self.ttl = (
            ttl
            if ttl is not None
            else getattr(settings, "COTURN_CREDENTIAL_TTL_SECONDS", 1800)
        )

        if not self.shared_secret:
            raise ImproperlyConfigured(
                "COTURN_SHARED_SECRET must be configured in settings."
            )

    def generate_credentials(
        self, participant_id: str, ttl: int | None = None
    ) -> dict[str, Any]:
        effective_ttl = ttl if ttl is not None else self.ttl
        expiry_timestamp = int(time.time()) + effective_ttl
        username = f"{expiry_timestamp}:{participant_id}"

        # Region 1 Credentials
        digester_r1 = hmac.new(
            key=self.shared_secret.encode("utf-8"),
            msg=username.encode("utf-8"),
            digestmod=hashlib.sha1,
        )
        password_r1 = base64.b64encode(digester_r1.digest()).decode("utf-8")

        ice_servers: list[dict[str, Any]] = [
            {"urls": [f"stun:{self.domain}:{self.port}"]},
            {
                "urls": [
                    f"turn:{self.domain}:{self.port}?transport=udp",
                    f"turn:{self.domain}:{self.port}?transport=tcp",
                ],
                "username": username,
                "credential": password_r1,
            },
            {
                "urls": [f"turns:{self.domain}:{self.tls_port}?transport=tcp"],
                "username": username,
                "credential": password_r1,
            },
        ]

        # Region 2 Credentials (T-36: Concurrent list for seamless failover)
        if self.region2_domain:
            digester_r2 = hmac.new(
                key=self.region2_shared_secret.encode("utf-8"),
                msg=username.encode("utf-8"),
                digestmod=hashlib.sha1,
            )
            password_r2 = base64.b64encode(digester_r2.digest()).decode("utf-8")

            ice_servers.extend(
                [
                    {"urls": [f"stun:{self.region2_domain}:{self.region2_port}"]},
                    {
                        "urls": [
                            f"turn:{self.region2_domain}:{self.region2_port}?transport=udp",
                            f"turn:{self.region2_domain}:{self.region2_port}?transport=tcp",
                        ],
                        "username": username,
                        "credential": password_r2,
                    },
                    {
                        "urls": [
                            f"turns:{self.region2_domain}:{self.region2_tls_port}?transport=tcp"
                        ],
                        "username": username,
                        "credential": password_r2,
                    },
                ]
            )

        regions = ["region-1-staging"]
        if self.region2_domain:
            regions.append("region-2-staging")

        return {
            "ice_servers": ice_servers,
            "iceServers": ice_servers,
            "ttl": effective_ttl,
            "expires_at": expiry_timestamp,
            "region": "multi-region" if self.region2_domain else "region-1-staging",
            "regions": regions,
            "fallback_only": False,
        }


def generate_ice_servers(
    participant_id: str,
    ttl: int | None = None,
    turn_secret: str | None = None,
    turn_urls: list[str] | None = None,
) -> dict[str, Any]:
    secret = (
        turn_secret
        if turn_secret is not None
        else (
            getattr(settings, "TURN_SHARED_SECRET", None)
            or getattr(settings, "COTURN_SHARED_SECRET", None)
        )
    )

    if not secret:
        stun_url = getattr(settings, "STUN_SERVER_URL", "stun:stun.l.google.com:19302")
        servers = [{"urls": [stun_url]}]
        return {
            "ice_servers": servers,
            "iceServers": servers,
            "fallback_only": True,
            "expires_at": None,
            "ttl": ttl or 0,
        }

    effective_turn_urls = (
        turn_urls if turn_urls is not None else getattr(settings, "TURN_URLS", None)
    )

    if effective_turn_urls:
        effective_ttl = ttl or 1800
        expiry_timestamp = int(time.time()) + effective_ttl
        username = f"{expiry_timestamp}:{participant_id}"
        digester = hmac.new(
            key=secret.encode("utf-8"),
            msg=username.encode("utf-8"),
            digestmod=hashlib.sha1,
        )
        password = base64.b64encode(digester.digest()).decode("utf-8")

        stun_url = getattr(settings, "STUN_SERVER_URL", "stun:stun.l.google.com:19302")
        servers = [
            {"urls": [stun_url]},
            {
                "urls": effective_turn_urls,
                "username": username,
                "credential": password,
            },
        ]
        return {
            "ice_servers": servers,
            "iceServers": servers,
            "fallback_only": False,
            "expires_at": expiry_timestamp,
            "ttl": effective_ttl,
        }

    service = TurnCredentialService(ttl=ttl)
    return service.generate_credentials(participant_id=participant_id, ttl=ttl)
