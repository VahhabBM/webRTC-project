"""Participant join-token and session authentication helpers."""

from __future__ import annotations

import hashlib
import logging
import secrets

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import Participant

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "p1_"
TOKEN_BYTES = 32
SESSION_PARTICIPANT_KEY = "participant_id"


class InvalidJoinToken(Exception):
    """The supplied token is malformed or not known."""


class ExpiredJoinToken(Exception):
    """The supplied token was valid but has expired."""


def event_end(participant: Participant):
    event = participant.event
    return event.start_time + (
        event.num_rounds * event.round_duration
        + max(event.num_rounds - 1, 0) * event.break_duration
    )


def issue_join_token(participant: Participant) -> str:
    """Rotate a participant token and return the raw token once, in memory."""
    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    participant.set_join_token(raw_token)
    participant.join_token_expires_at = event_end(participant)
    participant.save(
        update_fields=[
            "join_token_hash",
            "join_token_digest",
            "join_token_expires_at",
            "updated_at",
        ]
    )
    return raw_token


def personal_join_link(participant: Participant, raw_token: str | None = None) -> str:
    """Build a link without logging or persisting the raw token."""
    if raw_token is None:
        raise ValueError("A newly issued raw token is required to build a link")
    base = getattr(settings, "PARTICIPANT_JOIN_BASE_URL", "").rstrip("/")
    return f"{base}/join/{raw_token}/" if base else f"/join/{raw_token}/"


@transaction.atomic
def authenticate_join_token(raw_token: str) -> Participant:
    if not raw_token or not raw_token.startswith(TOKEN_PREFIX):
        raise InvalidJoinToken
    digest = hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
    participant = (
        Participant.objects.select_related("event")
        .filter(join_token_digest=digest)
        .first()
    )
    if participant is None or not participant.verify_join_token(raw_token):
        raise InvalidJoinToken
    if participant.token_is_expired():
        raise ExpiredJoinToken
    if participant.joined_at is None:
        participant.joined_at = timezone.now()
        participant.save(update_fields=["joined_at", "updated_at"])
    return participant


def establish_participant_session(request, participant: Participant) -> None:
    request.session.cycle_key()
    request.session[SESSION_PARTICIPANT_KEY] = str(participant.pk)
    request.session.set_expiry(
        getattr(settings, "PARTICIPANT_SESSION_AGE", 60 * 60 * 24 * 30)
    )


def resolve_participant_from_session(session) -> Participant | None:
    participant_id = session.get(SESSION_PARTICIPANT_KEY)
    if not participant_id:
        return None
    return Participant.objects.select_related("event").filter(pk=participant_id).first()


def resolve_participant_from_scope(scope) -> Participant | None:
    """T-14 hook: resolve identity from a scope containing Django session data."""
    session = scope.get("session")
    return resolve_participant_from_session(session) if session is not None else None
