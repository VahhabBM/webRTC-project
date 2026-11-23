from datetime import timedelta
import json
import pytest
from django.core import mail
from django.test import Client, RequestFactory
from django.contrib import admin
from django.contrib.auth.models import User
from django.utils import timezone

from apps.events.admin import EventAdmin
from apps.events.auth import SESSION_PARTICIPANT_KEY
from apps.events.email_notifications import (
    send_due_event_reminders_for_event,
    send_event_matches_summary_emails,
    send_registration_receipt_once,
)
from apps.events.interest import INTEREST_RESPONSE_WINDOW
from apps.events.live_dispatch import WAIT_MESSAGES
from apps.events.models import (
    DisconnectionLog,
    Event,
    EventStatus,
    Pair,
    Participant,
    RegistrationStatus,
)
from apps.events.views import leave_date
from apps.protocol.constants import WAITING_REASONS


@pytest.fixture
def test_event(db):
    return Event.objects.create(
        name="Sprint 2 Speed Dating",
        start_time=timezone.now() + timedelta(hours=5),
        num_rounds=5,
        status=EventStatus.SCHEDULED,
        reminder_lead_time=timedelta(hours=6),
        reminder_audience="female",
        early_exit_min_minutes=3,
    )


@pytest.mark.django_db
class TestSprint2Features:
    def test_task_1_instant_access_link_in_receipt(self, test_event):
        """Task 1: Confirmed registration includes the room access link immediately in receipt."""
        participant = Participant.objects.create(
            event=test_event,
            display_name="Sara",
            email="sara@example.com",
            gender="Female",
            registration_status=RegistrationStatus.CONFIRMED,
            registration_receipt_pending=True,
        )
        mail.outbox.clear()
        sent = send_registration_receipt_once(participant.pk)
        assert sent is True
        assert len(mail.outbox) == 1
        msg = mail.outbox[0]
        participant.refresh_from_db()
        assert participant.event_access_email_sent_at is not None
        assert "/join/" in msg.body or "/join/" in msg.alternatives[0][0]

    def test_task_1_and_6_event_reminders_and_audience(self, test_event):
        """Tasks 1 & 6: Event reminder sent according to lead_time and audience filter."""
        female_p = Participant.objects.create(
            event=test_event,
            display_name="Maryam",
            email="maryam@example.com",
            gender="Female",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        male_p = Participant.objects.create(
            event=test_event,
            display_name="Ali",
            email="ali@example.com",
            gender="Male",
            registration_status=RegistrationStatus.CONFIRMED,
        )

        mail.outbox.clear()
        count = send_due_event_reminders_for_event(test_event, now=timezone.now())
        assert count == 1
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["maryam@example.com"]

        female_p.refresh_from_db()
        assert female_p.reminder_email_sent_at is not None

        # If audience changed to 'male'
        test_event.reminder_audience = "male"
        test_event.save()
        mail.outbox.clear()
        count_male = send_due_event_reminders_for_event(test_event, now=timezone.now())
        assert count_male == 1
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["ali@example.com"]

    def test_task_5_no_match_emails_for_non_attendees(self, test_event):
        """Task 5: Participants who did not attend receive NO match summary email."""
        p_attended = Participant.objects.create(
            event=test_event,
            display_name="Attendee",
            email="attended@example.com",
            gender="Female",
            registration_status=RegistrationStatus.CONFIRMED,
            joined_at=timezone.now(),
        )
        p_noshow = Participant.objects.create(
            event=test_event,
            display_name="NoShow",
            email="noshow@example.com",
            gender="Male",
            registration_status=RegistrationStatus.CONFIRMED,
            joined_at=None,
        )

        mail.outbox.clear()
        sent_count = send_event_matches_summary_emails(test_event.pk)
        assert sent_count == 1
        assert len(mail.outbox) == 1
        assert mail.outbox[0].to == ["attended@example.com"]

    def test_task_8_sixty_second_interest_window(self):
        """Task 8: Partner interest response window is 60 seconds."""
        assert INTEREST_RESPONSE_WINDOW == timedelta(seconds=60)

    def test_task_3_leave_date_endpoint(self, test_event):
        """Task 3: Early exit endpoint logs disconnection accurately."""
        participant = Participant.objects.create(
            event=test_event,
            display_name="Reza",
            email="reza@example.com",
            gender="Male",
            registration_status=RegistrationStatus.CONFIRMED,
        )
        client = Client()
        session = client.session
        session[SESSION_PARTICIPANT_KEY] = str(participant.pk)
        session.save()

        resp = client.post(
            "/api/leave-date/",
            data=json.dumps({"room_id": "room-abc-123", "reason": "early_exit"}),
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = json.loads(resp.content)
        assert data.get("ok") is True

        log = DisconnectionLog.objects.filter(participant=participant).first()
        assert log is not None
        assert "room-abc-123" in log.detail

    def test_task_9_standby_protocol_and_messages(self):
        """Task 9: Standby reasons are part of protocol constants and wait messages."""
        assert "standby_fewer_women" in WAITING_REASONS
        assert "standby_fewer_men" in WAITING_REASONS
        assert "standby_fewer_women" in WAIT_MESSAGES
        assert "standby_fewer_men" in WAIT_MESSAGES

    def test_task_4_attendance_metrics_dashboard(self, test_event):
        """Task 4: Operations dashboard contains gender-separated attendance analytics."""
        Participant.objects.create(
            event=test_event,
            display_name="M1",
            gender="Male",
            registration_status=RegistrationStatus.CONFIRMED,
            joined_at=timezone.now(),
        )
        Participant.objects.create(
            event=test_event,
            display_name="M2",
            gender="Male",
            registration_status=RegistrationStatus.CONFIRMED,
            joined_at=None,
        )
        Participant.objects.create(
            event=test_event,
            display_name="F1",
            gender="Female",
            registration_status=RegistrationStatus.CONFIRMED,
            joined_at=timezone.now(),
        )

        user = User.objects.create_superuser("adminuser", "admin@example.com", "pass")
        rf = RequestFactory()
        req = rf.get("/admin/events/event/operations-dashboard/")
        req.user = user

        admin_obj = EventAdmin(Event, admin.site)
        response = admin_obj.operations_dashboard_view(req)
        assert response.status_code == 200
        metrics = response.context_data["attendance_metrics"]

        assert metrics["male"]["confirmed"] == 2
        assert metrics["male"]["attended"] == 1
        assert metrics["male"]["noshow"] == 1

        assert metrics["female"]["confirmed"] == 1
        assert metrics["female"]["attended"] == 1
        assert metrics["female"]["noshow"] == 0

        assert metrics["total"]["confirmed"] == 3
        assert metrics["total"]["attended"] == 2
        assert metrics["total"]["noshow"] == 1

    def test_confirmation_screen_instant_link_message(self):
        """Confirmation screen states the personal link is sent now, with no 9h reminder mention."""
        client = Client()
        response = client.get("/registration/check-email/?email=user@example.com")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Your personal event access link has been sent to your email." in content
        assert "9 hours" not in content
        assert "will be emailed" not in content

    def test_registration_stats_sorted_by_percentage_descending(self, test_event):
        """Registration stats by country/gender are sorted descending by count/percentage."""
        Participant.objects.create(event=test_event, display_name="C1", country="CA", gender="Female")
        Participant.objects.create(event=test_event, display_name="F1", country="FR", gender="Male")
        Participant.objects.create(event=test_event, display_name="F2", country="FR", gender="Male")
        for i in range(5):
            Participant.objects.create(event=test_event, display_name=f"U{i}", country="GB", gender="Female")

        user = User.objects.create_superuser("statadmin", "statadmin@example.com", "pass")
        client = Client()
        client.force_login(user)

        response = client.get(f"/admin/events/event/{test_event.pk}/registration-stats/")
        assert response.status_code == 200
        country_rows = response.context["country_rows"]
        assert country_rows[0]["count"] == 5
        assert country_rows[0]["label"] == "United Kingdom"
        assert country_rows[1]["count"] == 2
        assert country_rows[1]["label"] == "France"
        assert country_rows[2]["count"] == 1
        assert country_rows[2]["label"] == "Canada"
