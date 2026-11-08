"""Helpers for scheduled and manually requested event access emails."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.events.auth import event_end, issue_join_token
from apps.events.models import EventStatus, Participant, RegistrationStatus
from apps.registration.services.email_service import EmailService


def send_event_access_email_once(
    participant_id, *, allow_event_in_progress: bool = False
) -> bool:
    """Send a due room link once; optionally allow an event already in progress."""
    return _send_event_access_email(
        participant_id,
        allow_resend=False,
        allow_event_in_progress=allow_event_in_progress,
    )


def send_event_access_email_manually(participant_id) -> bool:
    """Send or resend a room link on an explicit administrator request."""
    return _send_event_access_email(
        participant_id,
        allow_resend=True,
        allow_event_in_progress=True,
    )


@transaction.atomic
def _send_event_access_email(
    participant_id, *, allow_resend: bool, allow_event_in_progress: bool
) -> bool:
    """Validate eligibility, rotate the link, and record a successful delivery."""
    participant = (
        Participant.objects.select_for_update()
        .select_related("event")
        .get(pk=participant_id)
    )
    now = timezone.now()
    if (
        (participant.event_access_email_sent_at and not allow_resend)
        or participant.joined_at is None
        or participant.email_verified_at is None
        or participant.registration_status != RegistrationStatus.CONFIRMED
        or not participant.email
        or participant.event.status
        not in {
            EventStatus.REGISTRATION_OPEN,
            EventStatus.SCHEDULED,
            EventStatus.LOCKED,
            EventStatus.ACTIVE,
            EventStatus.RUNNING,
        }
        or (participant.event.start_time <= now and not allow_event_in_progress)
        or (
            participant.event.start_time > now + timedelta(hours=24)
            and not allow_resend
        )
        or event_end(participant) <= now
    ):
        return False

    raw_token = issue_join_token(participant)
    join_url = EmailService.build_event_access_url(participant, raw_token)
    EmailService.send_event_access_email(
        recipient_email=participant.email,
        name=participant.display_name,
        event_title=participant.event.name,
        join_url=join_url,
    )

    participant.event_access_email_sent_at = now
    participant.save(update_fields=["event_access_email_sent_at", "updated_at"])
    return True
