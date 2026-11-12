from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.core import mail
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.auth import authenticate_join_token, issue_join_token
from apps.events.models import Event, EventStatus, Participant, RegistrationStatus
from apps.registration.models import EmailVerificationToken


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
        joined_at=None,
        email_verified_at=None,
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
def test_admin_access_link_action_resends_outside_window_and_during_event():
    _, outside_window = _verified_participant(start_delta=timedelta(days=2))
    previous_send = timezone.now() - timedelta(minutes=1)
    _, already_sent_during_event = _verified_participant(
        start_delta=-timedelta(minutes=10), email_sent_at=previous_send
    )
    already_sent_during_event.event.name = "Event already in progress"
    already_sent_during_event.event.save(update_fields=["name"])
    previous_token = issue_join_token(already_sent_during_event)

    response = _admin_client().post(
        reverse("admin:events_participant_changelist"),
        {
            "action": "send_selected_access_links",
            "_selected_action": [
                str(outside_window.pk),
                str(already_sent_during_event.pk),
            ],
        },
    )

    outside_window.refresh_from_db()
    already_sent_during_event.refresh_from_db()
    assert response.status_code == 302
    assert len(mail.outbox) == 2
    assert all("/join/" in message.body for message in mail.outbox)
    assert outside_window.event_access_email_sent_at is not None
    assert already_sent_during_event.event_access_email_sent_at > previous_send
    active_event_email = next(
        message
        for message in mail.outbox
        if message.subject == "Your access link for: Event already in progress"
    )
    new_token = active_event_email.body.split("/join/", 1)[1].split("/", 1)[0]
    assert authenticate_join_token(previous_token) == already_sent_during_event
    assert authenticate_join_token(new_token) == already_sent_during_event


@pytest.mark.django_db
def test_admin_access_action_does_not_resend_legacy_verification_email():
    event, participant = _verified_participant()
    participant.registration_status = RegistrationStatus.PENDING
    participant.joined_at = None
    participant.email_verified_at = None
    participant.reservation_expires_at = timezone.now() + timedelta(hours=2)
    participant.save(
        update_fields=[
            "registration_status",
            "joined_at",
            "email_verified_at",
            "reservation_expires_at",
        ]
    )
    old_token = EmailVerificationToken.objects.create(
        participant=participant,
        token="old-verification-token",
        expires_at=timezone.now() + timedelta(hours=1),
    )

    response = _admin_client().post(
        reverse("admin:events_participant_changelist"),
        {
            "action": "send_selected_access_links",
            "_selected_action": [str(participant.pk)],
        },
    )

    assert response.status_code == 302
    assert mail.outbox == []
    old_token.refresh_from_db()
    assert old_token.used_at is None
    participant.refresh_from_db()
    assert participant.registration_status == RegistrationStatus.PENDING
    assert participant.event_access_email_sent_at is None


@pytest.mark.django_db
def test_confirmed_unverified_unjoined_participant_can_receive_manual_access_link():
    event, participant = _verified_participant()
    participant.joined_at = None
    participant.email_verified_at = None
    participant.save(update_fields=["joined_at", "email_verified_at"])

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
    assert mail.outbox[0].subject == f"Your access link for: {event.name}"
    assert "/join/" in mail.outbox[0].body
    assert participant.joined_at is None
    assert participant.email_verified_at is None
    assert participant.event_access_email_sent_at is not None


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
    assert (
        b"Manually send room links or registration verification emails"
        not in response.content
    )
