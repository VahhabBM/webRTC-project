"""Time-limited, private contact-sharing choices for completed pairs."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.events.models import (
    DisconnectionCause,
    DisconnectionLog,
    Pair,
    PairInterest,
    PairStatus,
)

INTEREST_RESPONSE_WINDOW = timedelta(seconds=60)


class PairInterestError(Exception):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _choice_window(pair):
    """Early leave opens the choice immediately. A normal date waits for round end."""
    if pair.status == PairStatus.CANCELLED:
        left_at = (
            DisconnectionLog.objects.filter(
                event_id=pair.event_id,
                round_id=pair.round_id,
                cause=DisconnectionCause.TAB_CLOSED,
                participant_id__in=(pair.participant_a_id, pair.participant_b_id),
            )
            .order_by("-disconnected_at")
            .values_list("disconnected_at", flat=True)
            .first()
        )
        if left_at is not None:
            return left_at, left_at + INTEREST_RESPONSE_WINDOW
    end = pair.date_ends_at or pair.round.ends_at
    return end, end + INTEREST_RESPONSE_WINDOW


def submit_pair_interest(
    *, participant, room_id: str, round_number: int, interested: bool, now=None
) -> dict:
    """Record one immutable choice and reveal WhatsApp only after mutual yes."""
    current = now or timezone.now()

    with transaction.atomic():
        pair = (
            Pair.objects.select_for_update()
            .select_related(
                "event",
                "round",
                "participant_a",
                "participant_b",
            )
            .filter(
                room_id=room_id,
                round__number=round_number,
                event_id=participant.event_id,
            )
            .first()
        )
        if pair is None or participant.pk not in (
            pair.participant_a_id,
            pair.participant_b_id,
        ):
            raise PairInterestError(
                "pair_not_found", "This conversation could not be found.", 404
            )

        opened, deadline = _choice_window(pair)
        if current < opened:
            raise PairInterestError(
                "choice_not_open", "The choice window has not opened yet.", 409
            )
        if current > deadline:
            raise PairInterestError(
                "choice_expired", "The choice window has ended.", 410
            )

        choice = PairInterest.objects.select_for_update().filter(pair=pair).first()
        if choice is None:
            choice = PairInterest.objects.create(pair=pair)

        is_a = participant.pk == pair.participant_a_id
        response_field = (
            "participant_a_interested" if is_a else "participant_b_interested"
        )
        timestamp_field = (
            "participant_a_responded_at" if is_a else "participant_b_responded_at"
        )
        previous_response = getattr(choice, response_field)
        if previous_response is None:
            setattr(choice, response_field, interested)
            setattr(choice, timestamp_field, current)
            choice.save(update_fields=[response_field, timestamp_field, "updated_at"])
        elif previous_response is not interested:
            raise PairInterestError(
                "choice_already_recorded",
                "Your choice has already been recorded.",
                409,
            )

        partner = pair.participant_b if is_a else pair.participant_a
        return {
            "recorded": True,
            "mutual": choice.is_mutual,
            "partner_display_name": partner.display_name if choice.is_mutual else None,
            "partner_whatsapp": (partner.whatsapp or "") if choice.is_mutual else None,
        }
