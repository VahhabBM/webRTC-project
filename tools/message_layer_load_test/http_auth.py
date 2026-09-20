"""T-08 join-link authentication used by synthetic T-14 clients."""

from __future__ import annotations

import json
from http.cookiejar import CookieJar
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


class JoinAuthError(RuntimeError):
    """The existing join endpoint rejected or did not authenticate the client."""


def join_url(http_base: str, raw_token: str) -> str:
    base = http_base.rstrip("/")
    return f"{base}/join/{raw_token}/"


def session_cookie_from_jar(
    jar: CookieJar, cookie_name: str = "sessionid"
) -> str | None:
    for cookie in jar:
        if cookie.name == cookie_name:
            return cookie.value
    return None


def authenticate_join_token(
    http_base: str,
    raw_token: str,
    *,
    timeout: float = 15.0,
    cookie_name: str = "sessionid",
    opener_factory=None,
) -> dict[str, Any]:
    """GET ``/join/<token>/`` and return the Django session cookie.

    Uses the existing T-08 view. No alternate auth header or WS token is sent.
    The raw token is not returned.
    """
    if not raw_token:
        raise JoinAuthError("Join token is missing.")
    url = join_url(http_base, raw_token)
    jar = CookieJar()
    opener = (
        opener_factory
        or (lambda cookie_jar: build_opener(HTTPCookieProcessor(cookie_jar)))
    )(jar)
    request = Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            status = getattr(response, "status", 200)
    except HTTPError as exc:
        raise JoinAuthError(f"Join failed with HTTP {exc.code}.") from None
    except URLError as exc:
        raise JoinAuthError(f"Join request failed: {exc.reason}.") from None

    if status >= 400:
        raise JoinAuthError(f"Join failed with HTTP {status}.")

    try:
        payload = json.loads(body) if body else {}
    except json.JSONDecodeError:
        payload = {}
    if payload.get("authenticated") is not True:
        raise JoinAuthError("Join response was not authenticated.")

    cookie = session_cookie_from_jar(jar, cookie_name)
    if not cookie:
        raise JoinAuthError("Join succeeded but no session cookie was issued.")

    participant = payload.get("participant") or {}
    return {
        "session_cookie": cookie,
        "participant_id": str(participant.get("id") or ""),
        "event_id": str(participant.get("event_id") or ""),
        "display_name": str(participant.get("display_name") or ""),
    }
