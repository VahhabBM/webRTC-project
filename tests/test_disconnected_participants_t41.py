"""Tests for T-41: Disconnected Participants List & Incident Search."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.events.models import (
    DisconnectionCause,
    DisconnectionLog,
    Event,
    Participant,
    ParticipantIncidentNote,
    ParticipantStatus,
    Round,
)

User = get_user_model()


@pytest.fixture
def base_event():
    """ایجاد رویداد پایه با مقادیر معتبر DurationField و ستون number در Round."""
    now = timezone.now()
    event = Event.objects.create(
        name="T-41 Monitoring Event",
        num_rounds=3,
        round_duration=timedelta(minutes=3),
        break_duration=timedelta(seconds=30),
        start_time=now,
    )
    Round.objects.create(
        event=event,
        number=1,
        starts_at=now,
        ends_at=now + timedelta(minutes=3),
    )
    return event


@pytest.mark.django_db
def test_disconnection_causes_enum():
    """بررسی مقادیر استانداردهای علل سه‌گانه قطعی."""
    assert DisconnectionCause.NETWORK == "network"
    assert DisconnectionCause.TAB_CLOSED == "tab_closed"
    assert DisconnectionCause.PERMISSION_DENIED == "permission_denied"


@pytest.mark.django_db
def test_disconnection_log_creation_and_reconnect(base_event):
    """بررسی ثبت لاگ قطعی و ثبت زمان بازگشت کاربر."""
    participant = Participant.objects.create(
        event=base_event,
        display_name="Ali Reza",
        email="ali@example.com",
        status=ParticipantStatus.DISCONNECTED,
        join_token_hash="fake_hash_1",
    )
    disconnect_time = timezone.now()
    log = DisconnectionLog.objects.create(
        event=base_event,
        participant=participant,
        round=base_event.rounds.first(),
        cause=DisconnectionCause.TAB_CLOSED,
        detail="Client closed tab",
        disconnected_at=disconnect_time,
    )

    assert log.cause == "tab_closed"
    assert log.participant == participant
    assert log.reconnected_at is None
    assert participant.disconnection_logs.count() == 1

    # به‌روزرسانی زمان بازگشت
    reconnect_time = timezone.now()
    log.reconnected_at = reconnect_time
    log.save(update_fields=["reconnected_at"])

    log.refresh_from_db()
    assert log.reconnected_at == reconnect_time


@pytest.mark.django_db
def test_operator_monitoring_search_and_incident_note(base_event):
    """بررسی جستجوی نام/ایمیل و ثبت یادداشت رخداد توسط اپراتور."""
    operator_user = User.objects.create_superuser(
        "operator_admin", "admin@example.com", "pass123"
    )
    client = Client()
    client.force_login(operator_user)

    p1 = Participant.objects.create(
        event=base_event,
        display_name="Sara Ahmadi",
        email="sara@example.com",
        status=ParticipantStatus.DISCONNECTED,
        join_token_hash="fake_hash_sara",
    )
    p2 = Participant.objects.create(
        event=base_event,
        display_name="Babak Rad",
        email="babak@example.com",
        status=ParticipantStatus.ACTIVE,
        join_token_hash="fake_hash_babak",
    )

    DisconnectionLog.objects.create(
        event=base_event,
        participant=p1,
        cause=DisconnectionCause.NETWORK,
        disconnected_at=timezone.now(),
    )

    url = reverse("admin:events_event_live_monitoring", args=[base_event.pk])

    # ۱. مشاهده صفحه مانیتورینگ
    response = client.get(url)
    assert response.status_code == 200
    assert "Sara Ahmadi" in response.content.decode()

    # ۲. جستجو بر اساس نام
    search_name_resp = client.get(f"{url}?q=Sara")
    assert search_name_resp.status_code == 200
    assert "Sara Ahmadi" in search_name_resp.content.decode()
    assert p1 in search_name_resp.context["search_results"]
    assert p2 not in search_name_resp.context["search_results"]

    # ۳. جستجو بر اساس ایمیل
    search_email_resp = client.get(f"{url}?q=babak@example.com")
    assert search_email_resp.status_code == 200
    assert p2 in search_email_resp.context["search_results"]
    assert p1 not in search_email_resp.context["search_results"]

    # ۴. ثبت یادداشت رخداد
    note_resp = client.post(
        url,
        {
            "action": "add_incident_note",
            "participant_id": str(p1.pk),
            "note": "کاربر تماس گرفت؛ با قطع برق مودم خاموش شده بود.",
        },
    )
    assert note_resp.status_code == 302
    assert ParticipantIncidentNote.objects.filter(participant=p1).count() == 1

    note = ParticipantIncidentNote.objects.get(participant=p1)
    assert "مودم خاموش شده بود" in note.note
    assert note.operator == operator_user
    assert note.event == base_event
