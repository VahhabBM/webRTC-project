from datetime import timedelta

import pytest
from django.contrib.auth.hashers import make_password
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from apps.events.models import Event, EventStatus, Participant, RegistrationStatus


def _verified_participant(*, start_delta):
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
        joined_at=now,
        email_verified_at=now,
        registration_status=RegistrationStatus.CONFIRMED,
    )
    return event, participant


@pytest.mark.django_db
def test_due_access_email_is_sent_once_with_a_personal_room_link():
    event, participant = _verified_participant(start_delta=timedelta(hours=12))

    call_command("send_due_event_access_emails", confirm_production=True)

    participant.refresh_from_db()
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [participant.email]
    assert f"Your access link for: {event.name}" == mail.outbox[0].subject
    assert "/join/" in mail.outbox[0].body
    assert participant.event_access_email_sent_at is not None

    call_command("send_due_event_access_emails", confirm_production=True)
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_access_email_is_not_sent_more_than_24_hours_before_event():
    _event, participant = _verified_participant(start_delta=timedelta(days=2))

    call_command("send_due_event_access_emails", confirm_production=True)

    participant.refresh_from_db()
    assert not mail.outbox
    assert participant.event_access_email_sent_at is None
