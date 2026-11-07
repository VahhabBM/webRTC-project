from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from apps.events.models import (
    Event,
    EventRegistrationStatsSnapshot,
    EventStatus,
    Participant,
)
from apps.registration.models import DeviceCheckLog, TermsAcceptance


def _expired_event():
    return Event.objects.create(
        name="Expired event",
        status=EventStatus.COMPLETED,
        start_time=timezone.now() - timedelta(days=3),
    )


def _admin_client():
    user = get_user_model().objects.create_superuser(
        username="retention-admin",
        email="admin@example.com",
        password="test-pass",
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_expired_participant_records_are_deleted_after_stats_are_snapshotted():
    event = _expired_event()
    participants = [
        Participant.objects.create(
            event=event,
            display_name=f"Person {number}",
            email=f"person{number}@example.com",
            gender="Male" if number < 6 else "Female",
            country="DE" if number < 6 else "FR",
            whatsapp=f"+4912345678{number}",
        )
        for number in range(1, 7)
    ]
    terms = TermsAcceptance.objects.create(
        participant=participants[0],
        ip_address="192.0.2.10",
        consents={"terms_and_privacy": True},
    )
    DeviceCheckLog.objects.create(
        event=event,
        participant=participants[0],
        ip_address="192.0.2.11",
        user_agent="test browser",
    )

    call_command("delete_expired_registration_data", confirm_production=True)

    assert not Participant.objects.filter(event=event).exists()
    assert not TermsAcceptance.objects.filter(pk=terms.pk).exists()
    assert not DeviceCheckLog.objects.filter(event=event).exists()

    snapshot = EventRegistrationStatsSnapshot.objects.get(event=event)
    assert snapshot.total_registrations == 6
    assert snapshot.gender_counts == {"Male": 5, "__other__": 1}
    assert snapshot.country_counts == {"DE": 5, "__other__": 1}

    response = _admin_client().get(
        f"/admin/events/event/{event.pk}/registration-stats/"
    )
    html = response.content.decode()
    assert response.status_code == 200
    assert "Total registrations: 6" in html
    assert "These are event-level aggregate statistics" in html
    assert "Other (small groups)" in html
    assert "person1@example.com" not in html
    assert "+49123456781" not in html
    assert "Female" not in html
    assert "France" not in html

    call_command("delete_expired_registration_data", confirm_production=True)
    snapshot.refresh_from_db()
    assert snapshot.total_registrations == 6


@pytest.mark.django_db
def test_retention_dry_run_does_not_snapshot_or_delete_records():
    event = _expired_event()
    participant = Participant.objects.create(
        event=event,
        display_name="Person",
        email="person@example.com",
        gender="Female",
        country="FR",
    )

    call_command(
        "delete_expired_registration_data", dry_run=True, confirm_production=True
    )

    assert Participant.objects.filter(pk=participant.pk).exists()
    assert not EventRegistrationStatsSnapshot.objects.filter(event=event).exists()


@pytest.mark.django_db
def test_old_event_without_saved_statistics_is_not_reported_as_zero_registrations():
    event = _expired_event()
    response = _admin_client().get(
        f"/admin/events/event/{event.pk}/registration-stats/"
    )

    assert response.status_code == 200
    assert (
        b"No participant records or historical statistics are stored"
        in response.content
    )
    assert b"Total registrations: 0" not in response.content
