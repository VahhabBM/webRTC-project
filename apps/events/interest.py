"""Time-limited, private contact-sharing choices for completed pairs."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.events.models import Pair, PairInterest

INTEREST_RESPONSE_WINDOW = timedelta(seconds=30)


class PairInterestError(Exception):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


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

        deadline = pair.round.ends_at + INTEREST_RESPONSE_WINDOW
        if current < pair.round.ends_at:
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
