"""Short lived, authoritative WebSocket presence used by live matching."""

from __future__ import annotations

import logging
import time

from django.conf import settings
from django.core.cache import cache

from .orchestrator import connection_channel_key

logger = logging.getLogger(__name__)
_DEFAULT_TTL = 45


def _presence_key(participant_id) -> str:
    return f"ws:presence:{participant_id}"


def _ttl() -> int:
    return max(1, int(getattr(settings, "LIVE_PRESENCE_TTL_SECONDS", _DEFAULT_TTL)))


def publish_presence(participant, channel_name: str, ready: bool = False) -> bool:
    """Publish presence for the participant's currently claimed socket only."""
    if not participant or not channel_name:
        return False
    try:
        if cache.get(connection_channel_key(participant.pk)) != channel_name:
            return False
        cache.set(
            _presence_key(participant.pk),
            {"channel": channel_name, "ready": bool(ready), "touched": time.time()},
            _ttl(),
        )
        return True
    except Exception:
        logger.exception("Presence publish failed for participant %s", participant.pk)
        return False


def touch_presence(participant, channel_name: str, ready: bool | None = None) -> bool:
    """Refresh an owned presence lease, retaining its readiness by default."""
    if not participant or not channel_name:
        return False
    try:
        if cache.get(connection_channel_key(participant.pk)) != channel_name:
            return False
        key = _presence_key(participant.pk)
        current = cache.get(key)
        if current is not None and (
            not isinstance(current, dict) or current.get("channel") != channel_name
        ):
            return False
        # A current socket may resume after cache expiry. The caller supplies its
        # in-memory media-ready state so a ready lease can be restored safely.
        if not isinstance(current, dict):
            current = {"channel": channel_name, "ready": bool(ready)}
        if ready is not None:
            current["ready"] = bool(ready)
        current["touched"] = time.time()
        cache.set(key, current, _ttl())
        return True
    except Exception:
        logger.exception("Presence touch failed for participant %s", participant.pk)
        return False


def remove_presence(participant, channel_name: str) -> bool:
    """Remove presence only when this socket still owns the lease."""
    if not participant or not channel_name:
        return False
    try:
        key = _presence_key(participant.pk)
        current = cache.get(key)
        if (
            not isinstance(current, dict)
            or current.get("channel") != channel_name
            or cache.get(connection_channel_key(participant.pk)) != channel_name
        ):
            return False
        cache.delete(key)
        return True
    except Exception:
        logger.exception("Presence removal failed for participant %s", participant.pk)
        return False


def get_online_participant_ids(event, now=None) -> set[str]:
    """Return confirmed participants with a fresh ready lease for this event.

    The DB query is scoped to the event and bounded so cache failure or large
    registration lists cannot turn live allocation into an unbounded scan.
    """
    if event is None:
        return set()
    from .models import RegistrationStatus

    limit = max(1, int(getattr(settings, "LIVE_PRESENCE_MAX_CANDIDATES", 5000)))
    try:
        ids = list(
            event.participants.filter(registration_status=RegistrationStatus.CONFIRMED)
            .order_by("pk")
            .values_list("pk", flat=True)[:limit]
        )
        if not ids:
            return set()
        records = cache.get_many([_presence_key(pid) for pid in ids])
    except Exception:
        logger.exception("Presence lookup failed for event %s", getattr(event, "pk", None))
        return set()
    current = now.timestamp() if hasattr(now, "timestamp") else (float(now) if now is not None else time.time())
    ttl = _ttl()
    # Check current socket ownership in a batched read. A lease from a replaced
    # connection must never qualify, even if its own TTL has not elapsed.
    try:
        owners = cache.get_many([connection_channel_key(pid) for pid in ids])
    except Exception:
        logger.exception("Connection ownership lookup failed for event %s", getattr(event, "pk", None))
        return set()
    online = set()
    for pid in ids:
        record = records.get(_presence_key(pid))
        if not isinstance(record, dict) or not record.get("ready"):
            continue
        channel = record.get("channel")
        touched = record.get("touched")
        try:
            fresh = 0 <= current - float(touched) <= ttl
        except (TypeError, ValueError):
            fresh = False
        if channel and owners.get(connection_channel_key(pid)) == channel and fresh:
            online.add(str(pid))
    return online
