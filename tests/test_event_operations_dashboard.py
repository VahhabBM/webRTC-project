from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event, EventStatus, Participant, RegistrationStatus

User = get_user_model()


def _admin_client():
    user = User.objects.create_superuser(
        username="operations-admin",
        email="admin@example.com",
        password="test-pass",
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db
def test_operations_dashboard_shows_real_event_totals_and_tools():
    now = timezone.now()
    event = Event.objects.create(
        name="Organizer dashboard test",
        status=EventStatus.SCHEDULED,
        start_time=now + timedelta(days=2),
    )
    Participant.objects.create(
        event=event,
        display_name="Confirmed attendee",
        email="private@example.com",
        join_token_hash="test-hash",
        registration_status=RegistrationStatus.CONFIRMED,
    )
    Participant.objects.create(
        event=event,
        display_name="Waitlisted attendee",
        email="waitlisted@example.com",
        join_token_hash="test-hash",
        registration_status=RegistrationStatus.WAITLISTED,
    )
    other_event = Event.objects.create(
        name="Different event",
        status=EventStatus.SCHEDULED,
        start_time=now + timedelta(days=3),
    )
    Participant.objects.create(
        event=other_event,
        display_name="Other event attendee",
        email="other@example.com",
        join_token_hash="test-hash",
        registration_status=RegistrationStatus.CONFIRMED,
    )
    client = _admin_client()

    response = client.get(reverse("admin:events_event_operations_dashboard"))
    html = response.content.decode()

    assert response.status_code == 200
    assert "Organizer dashboard test" in html
    assert "Confirmed registrations" in html
    assert "Waitlisted registrations" in html
    assert "private@example.com" not in html
    assert "waitlisted@example.com" not in html
    assert reverse("admin:events_event_registration_stats", args=[event.pk]) in html
    assert (
        reverse("admin:events_event_registration_invitations", args=[event.pk]) in html
    )
    assert (
        f"{reverse('admin:events_participant_changelist')}?event__id__exact={event.pk}"
        in html
    )
    participant_list = client.get(
        f"{reverse('admin:events_participant_changelist')}?event__id__exact={event.pk}"
    )
    assert participant_list.status_code == 200
    assert "Confirmed attendee" in participant_list.content.decode()
    assert "Other event attendee" not in participant_list.content.decode()

    event_list = client.get(reverse("admin:events_event_changelist"))
    assert b"Operations Dashboard" in event_list.content


@pytest.mark.django_db
def test_operations_dashboard_requires_event_view_permission():
    staff = User.objects.create_user(
        username="staff-without-event-view",
        password="test-pass",
        is_staff=True,
    )
    client = Client()
    client.force_login(staff)

    response = client.get(reverse("admin:events_event_operations_dashboard"))

    assert response.status_code == 403
