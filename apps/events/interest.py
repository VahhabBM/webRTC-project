"""Time-limited, private contact-sharing choices for completed pairs."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.events.models import Pair, PairInterest
from apps.registration.services.email_service import EmailService

logger = logging.getLogger(__name__)

INTEREST_RESPONSE_WINDOW = timedelta(seconds=30)


class PairInterestError(Exception):
    def __init__(self, code: str, message: str, status_code: int):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def submit_pair_interest(
    *, participant, room_id: str, round_number: int, interested: bool, now=None
) -> dict:
    """Record one immutable choice and send both emails only for mutual yes."""
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

        if choice.is_mutual:
            _deliver_mutual_emails(choice, pair, current)

        own_email_sent_at = (
            choice.email_sent_to_a_at if is_a else choice.email_sent_to_b_at
        )
        return {
            "recorded": True,
            "mutual": choice.is_mutual,
            "email_sent": own_email_sent_at is not None,
            "email_pending": choice.is_mutual
            and (
                choice.email_sent_to_a_at is None or choice.email_sent_to_b_at is None
            ),
        }


def _deliver_mutual_emails(choice: PairInterest, pair: Pair, now) -> None:
    if not choice.is_mutual:
        return

    deliveries = (
        (
            "email_sent_to_a_at",
            pair.participant_a,
            pair.participant_b,
        ),
        (
            "email_sent_to_b_at",
            pair.participant_b,
            pair.participant_a,
        ),
    )
    for sent_field, recipient, partner in deliveries:
        if getattr(choice, sent_field) is not None:
            continue
        if not recipient.email:
            logger.error(
                "Mutual-interest email skipped because participant %s has no email",
                recipient.pk,
            )
            continue
        try:
            EmailService.send_mutual_interest_email(
                recipient_email=recipient.email,
                recipient_name=recipient.display_name,
                partner_name=partner.display_name,
                partner_email=partner.email,
                partner_whatsapp=partner.whatsapp,
                event_title=pair.event.name,
            )
        except Exception:
            logger.exception(
                "Mutual-interest email failed for pair %s and participant %s",
                pair.pk,
                recipient.pk,
            )
            continue

        setattr(choice, sent_field, now)
        choice.save(update_fields=[sent_field, "updated_at"])
