"""Participant join-token and session authentication helpers."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta

from django.conf import settings

# Example if using django.core.signing:
from django.core.cache import cache
from django.core.signing import BadSignature, SignatureExpired, TimestampSigner
from django.db import transaction
from django.utils import timezone

from .models import Participant, ParticipantAccessToken, RegistrationStatus

logger = logging.getLogger(__name__)

TOKEN_PREFIX = "p1_"
TOKEN_BYTES = 32
SESSION_PARTICIPANT_KEY = "participant_id"


def participant_session_cache_key(participant_id) -> str:
    return f"participant:session:{participant_id}"


class InvalidJoinToken(Exception):
    """The supplied token is malformed or not known."""


class ExpiredJoinToken(Exception):
    """The supplied token was valid but has expired."""


def event_end(participant: Participant):
    return scheduled_event_end(participant.event)


def scheduled_event_end(event):
    intro_offset = (
        timedelta(seconds=event.intro_video_duration_seconds + 20)
        if event.intro_video_url
        else timedelta(0)
    )
    return event.start_time + intro_offset + (
        event.num_rounds * event.round_duration
        + max(event.num_rounds - 1, 0) * event.break_duration
    )


def issue_join_token(participant: Participant) -> str:
    """Issue a fresh token while retaining the previously emailed token."""
    previous_expiry = event_end(participant)
    if participant.join_token_digest and participant.join_token_hash:
        previous_token, _ = ParticipantAccessToken.objects.get_or_create(
            digest=participant.join_token_digest,
            defaults={
                "participant": participant,
                "token_hash": participant.join_token_hash,
                "expires_at": previous_expiry,
            },
        )
        if previous_token.expires_at < previous_expiry:
            previous_token.expires_at = previous_expiry
            previous_token.save(update_fields=["expires_at"])

    raw_token = TOKEN_PREFIX + secrets.token_urlsafe(TOKEN_BYTES)
    participant.set_join_token(raw_token)
    participant.join_token_expires_at = previous_expiry
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
    retained_token = (
        ParticipantAccessToken.objects.select_related(
            "participant", "participant__event"
        )
        .filter(digest=digest)
        .first()
    )
    if retained_token is not None:
        participant = retained_token.participant
        if (
            participant.registration_status != RegistrationStatus.CONFIRMED
            or not retained_token.verify(raw_token)
        ):
            raise InvalidJoinToken
        if retained_token.expires_at <= timezone.now():
            raise ExpiredJoinToken
    else:
        participant = (
            Participant.objects.select_related("event")
            .filter(join_token_digest=digest)
            .first()
        )
        if (
            participant is None
            or participant.registration_status != RegistrationStatus.CONFIRMED
            or not participant.verify_join_token(raw_token)
        ):
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
    request.session.save()
    remember_participant_session(participant.pk, request.session.session_key)


def remember_participant_session(participant_id, session_key: str | None) -> None:
    """T-08/T-34: only the latest personal-link session may use this identity."""
    if not session_key:
        return
    try:
        cache.set(
            participant_session_cache_key(participant_id),
            session_key,
            timeout=getattr(settings, "PARTICIPANT_SESSION_AGE", 60 * 60 * 24 * 30),
        )
    except Exception:
        logger.exception(
            "Failed recording current session for participant %s", participant_id
        )


def session_matches_participant(session, participant: Participant) -> bool:
    try:
        current = cache.get(participant_session_cache_key(participant.pk))
    except Exception:
        logger.exception(
            "Failed reading current session for participant %s", participant.pk
        )
        return True
    if not current:
        return True
    return session.session_key == current


def resolve_participant_from_session(session) -> Participant | None:
    participant_id = session.get(SESSION_PARTICIPANT_KEY)
    if not participant_id:
        return None
    participant = (
        Participant.objects.select_related("event").filter(pk=participant_id).first()
    )
    if participant is None:
        return None
    if not session_matches_participant(session, participant):
        return None
    return participant


def resolve_participant_from_scope(scope) -> Participant | None:
    """T-14 hook: resolve identity from a scope containing Django session data."""
    session = scope.get("session")
    return resolve_participant_from_session(session) if session is not None else None


def verify_join_token(token: str, max_age: int = 3600) -> dict | None:
    signer = TimestampSigner()
    try:
        data = signer.unsign_object(token, max_age=max_age)
        return data
    except (BadSignature, SignatureExpired):
        return None
