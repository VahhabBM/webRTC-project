"""Helpers for scheduled and manually requested event access emails."""

import logging
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.events.auth import event_end, issue_join_token
from apps.events.models import Event, EventStatus, Pair, Participant, RegistrationStatus
from apps.registration.services.email_service import EmailService

logger = logging.getLogger(__name__)

EVENT_ACCESS_LEAD_HOURS = 9


@transaction.atomic
def send_registration_receipt_once(participant_id) -> bool:
    """Deliver an explicitly queued receipt; leave failed sends retryable.
    
    For confirmed participants, immediately attaches their personal event access link
    so only one comprehensive confirmation email is sent upon registration.
    """
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

    join_url = None
    now = timezone.now()
    if participant.registration_status == RegistrationStatus.CONFIRMED:
        raw_token = issue_join_token(participant)
        join_url = EmailService.build_event_access_url(participant, raw_token)
        participant.event_access_email_sent_at = now

    EmailService.send_registration_receipt_email(
        recipient_email=participant.email,
        name=participant.display_name,
        event_title=participant.event.name,
        registration_status=participant.registration_status,
        join_url=join_url,
    )
    participant.registration_receipt_pending = False
    participant.registration_receipt_sent_at = now
    participant.save(
        update_fields=[
            "registration_receipt_pending",
            "registration_receipt_sent_at",
            "event_access_email_sent_at",
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


def send_due_event_reminders_for_event(event, *, now=None) -> int:
    """Send reminder emails when within event.reminder_lead_time of start_time."""
    if not event.reminder_lead_time or event.reminder_lead_time.total_seconds() <= 0:
        return 0
    now = now or timezone.now()
    cutoff = event.start_time - event.reminder_lead_time
    if now < cutoff or now >= event.start_time:
        return 0

    qs = (
        Participant.objects.select_for_update()
        .filter(
            event=event,
            registration_status=RegistrationStatus.CONFIRMED,
            reminder_email_sent_at__isnull=True,
        )
        .exclude(email="")
    )

    audience = getattr(event, "reminder_audience", "female")
    if audience == "female":
        qs = qs.filter(gender__iexact="female")
    elif audience == "male":
        qs = qs.filter(gender__iexact="male")

    remaining_seconds = max(0, int((event.start_time - now).total_seconds()))
    hours_remaining = round(remaining_seconds / 3600)
    time_text = (
        f"{hours_remaining} hours"
        if hours_remaining > 0
        else f"{max(1, round(remaining_seconds / 60))} minutes"
    )

    delivered = 0
    for participant in list(qs):
        try:
            raw_token = issue_join_token(participant)
            join_url = EmailService.build_event_access_url(participant, raw_token)
            EmailService.send_event_reminder_email(
                recipient_email=participant.email,
                name=participant.display_name,
                event_title=event.name,
                join_url=join_url,
                time_until_start=time_text,
            )
            participant.reminder_email_sent_at = now
            participant.save(update_fields=["reminder_email_sent_at", "updated_at"])
            delivered += 1
        except Exception:
            logger.exception("Failed sending reminder email to %s", participant.email)

    return delivered


@transaction.atomic
def send_event_matches_summary_emails(event_id, *, force_resend: bool = False) -> int:
    """Send one consolidated email with all mutual matches to each attendee of the event.
    
    Excludes participants who did not attend / never connected to the room.
    """
    event = Event.objects.get(pk=event_id)
    participants_qs = (
        Participant.objects.select_for_update()
        .filter(event=event)
        .exclude(email="")
    )
    if not force_resend:
        participants_qs = participants_qs.filter(matches_email_sent_at__isnull=True)

    pairs_pids = set(
        Pair.objects.filter(event=event)
        .exclude(status="cancelled")
        .values_list("participant_a_id", flat=True)
    ) | set(
        Pair.objects.filter(event=event)
        .exclude(status="cancelled")
        .values_list("participant_b_id", flat=True)
    )

    participants = list(participants_qs)
    now = timezone.now()
    delivered_count = 0

    for participant in participants:
        has_attended = bool(participant.joined_at) or (participant.pk in pairs_pids)
        if not has_attended:
            # Skip non-attendees / no-shows completely (Task 5)
            continue

        pairs = (
            Pair.objects.filter(
                event=event,
                interest__participant_a_interested=True,
                interest__participant_b_interested=True,
            )
            .filter(Q(participant_a=participant) | Q(participant_b=participant))
            .select_related("participant_a", "participant_b")
        )
        matches = []
        for pair in pairs:
            partner = (
                pair.participant_b
                if pair.participant_a_id == participant.pk
                else pair.participant_a
            )
            matches.append(
                {
                    "name": partner.display_name,
                    "whatsapp": partner.whatsapp or "",
                }
            )

        try:
            EmailService.send_matches_summary_email(
                recipient_email=participant.email,
                name=participant.display_name,
                event_title=event.name,
                matches=matches,
            )
            participant.matches_email_sent_at = now
            participant.save(update_fields=["matches_email_sent_at", "updated_at"])
            delivered_count += 1
        except Exception:
            logger.exception(
                "Failed to send matches summary email to %s for event %s",
                participant.email,
                event.pk,
            )

    return delivered_count
