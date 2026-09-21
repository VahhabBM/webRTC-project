"""Machine-readable T-44 result file helpers. Never persist secrets."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SECRET_KEY_MARKERS = (
    "token",
    "cookie",
    "session",
    "password",
    "secret",
    "authorization",
    "credential",
    "join_link",
)
_TOKEN_RE = re.compile(r"p1_[A-Za-z0-9_\-]{8,}")
_JOIN_RE = re.compile(r"/join/[^/\s\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_SESSION_RE = re.compile(r"sessionid=[^;\s\"']+", re.IGNORECASE)


def is_secret_key(key: str) -> bool:
    lowered = str(key).lower()
    return any(marker in lowered for marker in SECRET_KEY_MARKERS)


def redact_text(value: str) -> str:
    text = _TOKEN_RE.sub("[REDACTED]", value)
    text = _JOIN_RE.sub("/join/[REDACTED]", text)
    text = _BEARER_RE.sub("Bearer [REDACTED]", text)
    return _SESSION_RE.sub("sessionid=[REDACTED]", text)


def strip_secrets(value: Any) -> Any:
    """Recursively drop/redact tokens, cookies, and join URLs."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if is_secret_key(str(key)):
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = strip_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [strip_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [strip_secrets(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def write_result_json(path: str | Path, payload: dict[str, Any]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    cleaned = strip_secrets(payload)
    output.write_text(
        json.dumps(cleaned, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return output
