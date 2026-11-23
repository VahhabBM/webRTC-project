from datetime import timedelta
from unittest.mock import patch

import pytest
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.events.models import Event, EventStatus, Participant, RegistrationStatus


def _confirmed_participant(*, start_delta):
    now = timezone.now()
    event = Event.objects.create(
        name="Access email test",
        status=EventStatus.REGISTRATION_OPEN,
        start_time=now + start_delta,
    )
    participant = Participant.objects.create(
        event=event,
        display_name="Test participant",
        email="participant@example.com",
        gender="Female",
        join_token_hash=make_password("previous-token"),
        registration_status=RegistrationStatus.CONFIRMED,
    )
    return event, participant


@pytest.mark.django_db
def test_due_access_email_is_sent_once_with_a_personal_room_link():
    event, participant = _confirmed_participant(start_delta=timedelta(hours=8))

    call_command("send_due_event_access_emails", confirm_production=True)

    participant.refresh_from_db()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [participant.email]
    assert f"Your access link for: {event.name}" == mail.outbox[0].subject
    assert "/join/" in mail.outbox[0].body
    assert participant.event_access_email_sent_at is not None
    assert participant.joined_at is None
    assert participant.email_verified_at is None

    call_command("send_due_event_access_emails", confirm_production=True)
    assert len(mail.outbox) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(
    "start_delta",
    [timedelta(hours=9, minutes=1), timedelta(hours=12), timedelta(days=2)],
)
def test_access_email_is_not_sent_more_than_9_hours_before_event(start_delta):
    _event, participant = _confirmed_participant(start_delta=start_delta)

    call_command("send_due_event_access_emails", confirm_production=True)

    participant.refresh_from_db()
    assert not mail.outbox
    assert participant.event_access_email_sent_at is None


@pytest.mark.django_db
def test_late_confirmed_registration_receives_access_email_once_while_event_runs():
    event, participant = _confirmed_participant(start_delta=timedelta(minutes=-5))
    event.round_duration = timedelta(minutes=20)
    event.save(update_fields=["round_duration"])
    assert participant.joined_at is None
    assert participant.email_verified_at is None

    call_command("send_due_event_access_emails", confirm_production=True)

    participant.refresh_from_db()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [participant.email]
    assert mail.outbox[0].subject == f"Your access link for: {event.name}"
    assert participant.event_access_email_sent_at is not None
    assert participant.joined_at is None
    assert participant.email_verified_at is None

    call_command("send_due_event_access_emails", confirm_production=True)
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_ended_event_does_not_send_access_email():
    _event, participant = _confirmed_participant(start_delta=timedelta(days=-2))

    call_command("send_due_event_access_emails", confirm_production=True)

    participant.refresh_from_db()
    assert mail.outbox == []
    assert participant.event_access_email_sent_at is None


@pytest.mark.django_db
def test_failed_access_email_remains_retryable():
    _event, participant = _confirmed_participant(start_delta=timedelta(hours=8))

    with patch(
        "apps.registration.services.email_service.EmailService.send_event_access_email",
        side_effect=RuntimeError("temporary SMTP outage"),
    ):
        with pytest.raises(CommandError, match="Failed to send 1"):
            call_command("send_due_event_access_emails", confirm_production=True)

    participant.refresh_from_db()
    assert participant.event_access_email_sent_at is None
    assert mail.outbox == []

    call_command("send_due_event_access_emails", confirm_production=True)

    participant.refresh_from_db()
    assert len(mail.outbox) == 1
    assert participant.event_access_email_sent_at is not None
