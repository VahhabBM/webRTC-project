import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core import mail
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from apps.events.email_notifications import (
    send_event_access_email_once,
    send_registration_receipt_once,
)
from apps.events.models import Event, EventStatus, Participant
from apps.registration.models import EmailVerificationToken


def _register(client, event):
    return client.post(
        reverse("registration:register", args=[event.pk]),
        data=json.dumps(
            {
                "display_name": "Test Person",
                "email": "person@example.com",
                "email_confirmation": "person@example.com",
                "age": 29,
                "country": "NZ",
                "tag_ids": [],
                "consents": {
                    key: True
                    for key in (
                        "age_confirmation",
                        "sensitive_data_processing",
                        "mutual_contact_sharing",
                        "terms_and_privacy",
                    )
                },
            }
        ),
        content_type="application/json",
    )


@pytest.fixture
def event(db):
    return Event.objects.create(
        name="Receipt check",
        status=EventStatus.REGISTRATION_OPEN,
        start_time=timezone.now() + timedelta(days=2),
    )


@pytest.mark.django_db
def test_receipt_failure_keeps_registration_and_retries_without_duplicates(
    client, event
):
    with patch(
        "apps.registration.services.email_service.send_mail",
        side_effect=OSError("SMTP unavailable"),
    ):
        response = _register(client, event)
    assert response.status_code == 201
    participant = Participant.objects.get(event=event)
    assert participant.registration_status == "confirmed"
    assert participant.registration_receipt_pending
    assert participant.registration_receipt_sent_at is None
    assert not EmailVerificationToken.objects.filter(participant=participant).exists()
    call_command("send_due_event_access_emails", confirm_production=True)
    participant.refresh_from_db()
    assert not participant.registration_receipt_pending
    assert participant.registration_receipt_sent_at
    assert len(mail.outbox) == 1
    assert "/verify/" not in mail.outbox[0].body
    assert "/join/" not in mail.outbox[0].body
    call_command("send_due_event_access_emails", confirm_production=True)
    assert len(mail.outbox) == 1
    assert send_registration_receipt_once(participant.pk) is False
    assert _register(client, event).status_code == 400
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_waitlist_receipt_does_not_claim_a_confirmed_place(client, event, settings):
    settings.EVENT_REGISTRATION_CAPACITY = 0
    assert _register(client, event).status_code == 201
    assert len(mail.outbox) == 1
    assert "waitlist" in mail.outbox[0].body
    assert "not yet confirmed" in mail.outbox[0].body
    assert "/join/" not in mail.outbox[0].body


@pytest.mark.django_db
def test_existing_registrations_do_not_get_unsolicited_receipt_backfill(event):
    Participant.objects.create(
        event=event, display_name="Existing", email="old@example.com"
    )
    call_command("send_due_event_access_emails", confirm_production=True)
    assert not mail.outbox


@pytest.mark.django_db
def test_access_threshold_is_exactly_nine_hours(event):
    now = timezone.now()
    event.start_time = now + timedelta(hours=9)
    event.save(update_fields=["start_time"])
    participant = Participant.objects.create(
        event=event, display_name="Boundary", email="boundary@example.com"
    )
    with patch(
        "apps.events.email_notifications.timezone.now",
        return_value=now - timedelta(microseconds=1),
    ):
        assert send_event_access_email_once(participant.pk) is False
    assert not mail.outbox
    with patch("apps.events.email_notifications.timezone.now", return_value=now):
        assert send_event_access_email_once(participant.pk) is True
    assert len(mail.outbox) == 1


@pytest.mark.django_db
def test_zero_email_delivery_stays_queued(client, event):
    with patch("apps.registration.services.email_service.send_mail", return_value=0):
        assert _register(client, event).status_code == 201
    participant = Participant.objects.get(event=event)
    assert participant.registration_receipt_pending
    assert participant.registration_receipt_sent_at is None


@pytest.mark.django_db
def test_late_registration_gets_receipt_then_separate_room_link(client, event):
    event.start_time = timezone.now() + timedelta(hours=1)
    event.save(update_fields=["start_time"])
    assert _register(client, event).status_code == 201
    assert len(mail.outbox) == 1
    assert "Registration complete" in mail.outbox[0].subject
    assert "/join/" not in mail.outbox[0].body
    call_command("send_due_event_access_emails", confirm_production=True)
    assert len(mail.outbox) == 2
    assert "Your access link" in mail.outbox[1].subject
    assert "/join/" in mail.outbox[1].body
    assert EmailVerificationToken.objects.count() == 0


@pytest.mark.django_db(transaction=True)
def test_competing_receipt_workers_send_only_once():
    from concurrent.futures import ThreadPoolExecutor

    from django.db import close_old_connections, connection

    if connection.vendor != "postgresql":
        pytest.skip("Receipt row-lock concurrency requires PostgreSQL")
    event = Event.objects.create(
        name="Concurrent receipt", start_time=timezone.now() + timedelta(days=2)
    )
    participant = Participant.objects.create(
        event=event,
        display_name="Concurrent",
        email="concurrent@example.com",
        registration_receipt_pending=True,
    )

    def deliver():
        close_old_connections()
        try:
            return send_registration_receipt_once(participant.pk)
        finally:
            connection.close()

    with patch(
        "apps.registration.services.email_service.EmailService.send_registration_receipt_email"
    ) as send:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: deliver(), range(2)))
    assert sorted(results) == [False, True]
    assert send.call_count == 1
