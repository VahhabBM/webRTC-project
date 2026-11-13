"""Helpers for scheduled and manually requested event access emails."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.events.auth import event_end, issue_join_token
from apps.events.models import EventStatus, Participant, RegistrationStatus
from apps.registration.services.email_service import EmailService

EVENT_ACCESS_LEAD_HOURS = 9


@transaction.atomic
def send_registration_receipt_once(participant_id) -> bool:
    """Deliver an explicitly queued receipt; leave failed sends retryable."""
    participant = (
        Participant.objects.select_for_update()
        .select_related("event")
        .get(pk=participant_id)
    )
    if (
        not participant.registration_receipt_pending
        or participant.registration_receipt_sent_at
        or not participant.email
        or participant.registration_status
        not in {
            RegistrationStatus.CONFIRMED,
            RegistrationStatus.WAITLISTED,
        }
    ):
        return False
    EmailService.send_registration_receipt_email(
        recipient_email=participant.email,
        name=participant.display_name,
        event_title=participant.event.name,
        registration_status=participant.registration_status,
    )
    participant.registration_receipt_pending = False
    participant.registration_receipt_sent_at = timezone.now()
    participant.save(
        update_fields=[
            "registration_receipt_pending",
            "registration_receipt_sent_at",
            "updated_at",
        ]
    )
    return True


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
            participant.event.start_time
            > now + timedelta(hours=EVENT_ACCESS_LEAD_HOURS)
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
