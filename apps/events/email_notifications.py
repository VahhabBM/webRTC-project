"""Helpers for one-time event access email delivery."""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.events.auth import event_end, issue_join_token
from apps.events.models import EventStatus, Participant
from apps.registration.services.email_service import EmailService


@transaction.atomic
def send_event_access_email_once(participant_id) -> bool:
    """Send a participant's room link once and record the successful delivery."""
    participant = (
        Participant.objects.select_for_update()
        .select_related("event")
        .get(pk=participant_id)
    )
    now = timezone.now()
    if (
        participant.event_access_email_sent_at
        or participant.joined_at is None
        or not participant.email
        or participant.event.status
        not in {
            EventStatus.REGISTRATION_OPEN,
            EventStatus.SCHEDULED,
            EventStatus.LOCKED,
            EventStatus.ACTIVE,
        }
        or participant.event.start_time <= now
        or participant.event.start_time > now + timedelta(hours=24)
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
