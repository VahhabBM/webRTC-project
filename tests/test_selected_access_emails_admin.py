from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event, EventStatus, Participant, RegistrationStatus


def _admin_client():
    user = get_user_model().objects.create_superuser(
        username="access-link-admin",
        email="admin@example.com",
        password="test-pass",
    )
    client = Client()
    client.force_login(user)
    return client


def _verified_participant(*, start_delta=timedelta(hours=12), email_sent_at=None):
    now = timezone.now()
    event = Event.objects.create(
        name="Selected access-link test",
        status=EventStatus.REGISTRATION_OPEN,
        start_time=now + start_delta,
    )
    participant = Participant.objects.create(
        event=event,
        display_name="Test participant",
        email="participant@example.com",
        gender="Female",
        join_token_hash="previous-token-hash",
        joined_at=now,
        email_verified_at=now,
        registration_status=RegistrationStatus.CONFIRMED,
        event_access_email_sent_at=email_sent_at,
    )
    return event, participant


@pytest.mark.django_db
def test_admin_can_send_personal_access_link_to_selected_eligible_participant():
    event, participant = _verified_participant()
    response = _admin_client().post(
        reverse("admin:events_participant_changelist"),
        {
            "action": "send_selected_access_links",
            "_selected_action": [str(participant.pk)],
        },
    )

    participant.refresh_from_db()
    assert response.status_code == 302
    assert len(mail.outbox) == 1
    assert mail.outbox[0].to == [participant.email]
    assert f"Your access link for: {event.name}" == mail.outbox[0].subject
    assert "/join/" in mail.outbox[0].body
    assert participant.event_access_email_sent_at is not None


@pytest.mark.django_db
def test_admin_access_link_action_skips_out_of_window_and_already_sent_participants():
    _, too_early = _verified_participant(start_delta=timedelta(days=2))
    _, already_sent = _verified_participant(
        email_sent_at=timezone.now() - timedelta(minutes=1)
    )

    response = _admin_client().post(
        reverse("admin:events_participant_changelist"),
        {
            "action": "send_selected_access_links",
            "_selected_action": [str(too_early.pk), str(already_sent.pk)],
        },
    )

    too_early.refresh_from_db()
    assert response.status_code == 302
    assert not mail.outbox
    assert too_early.event_access_email_sent_at is None


@pytest.mark.django_db
def test_admin_access_link_action_limits_each_send_to_50_people():
    event, _ = _verified_participant()
    participants = [
        Participant.objects.create(
            event=event,
            display_name=f"Test participant {number}",
            email=f"participant{number}@example.com",
            gender="Female",
            join_token_hash="previous-token-hash",
            joined_at=timezone.now(),
            email_verified_at=timezone.now(),
            registration_status=RegistrationStatus.CONFIRMED,
        )
        for number in range(51)
    ]
    response = _admin_client().post(
        reverse("admin:events_participant_changelist"),
        {
            "action": "send_selected_access_links",
            "_selected_action": [str(participant.pk) for participant in participants],
        },
    )

    assert response.status_code == 302
    assert not mail.outbox
    assert not Participant.objects.filter(
        pk__in=[participant.pk for participant in participants],
        event_access_email_sent_at__isnull=False,
    ).exists()


@pytest.mark.django_db
def test_view_only_staff_cannot_send_selected_access_links():
    user = get_user_model().objects.create_user(
        username="view-only-participant-admin",
        password="test-pass",
        is_staff=True,
    )
    permission = Permission.objects.get(
        content_type__app_label="events", codename="view_participant"
    )
    user.user_permissions.add(permission)
    client = Client()
    client.force_login(user)

    response = client.get(reverse("admin:events_participant_changelist"))

    assert response.status_code == 200
    assert b"Send event access links to selected participants" not in response.content
