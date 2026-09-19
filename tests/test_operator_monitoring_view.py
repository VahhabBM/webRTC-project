from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse
from django.utils import timezone

from apps.events.models import Event, OperatorActionLog, OperatorActionType, Round
from apps.events.scheduler import RoundScheduler

User = get_user_model()


@pytest.mark.django_db
class TestOperatorMonitoringView:
    @pytest.fixture
    def setup_data(self):
        admin = User.objects.create_superuser("admin", "admin@test.com", "pass1234")
        now = timezone.now()
        event = Event.objects.create(
            name="Live Monitoring Test Event",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=now,
        )
        RoundScheduler(event).persist_schedule()
        return admin, event

    def test_unauthenticated_user_redirected(self, client, setup_data):
        _, event = setup_data
        url = reverse("admin:events_event_live_monitoring", args=[event.pk])
        response = client.get(url)
        assert response.status_code == 302

    def test_admin_get_live_monitoring_renders_ok(self, client, setup_data):
        admin, event = setup_data
        client.force_login(admin)
        url = reverse("admin:events_event_live_monitoring", args=[event.pk])
        response = client.get(url)
        assert response.status_code == 200
        content = response.content.decode()
        assert "Live Monitoring" in content
        assert event.name in content
        assert "Round Controls (T-40)" in content

    def test_change_form_contains_live_monitoring_button(self, client, setup_data):
        admin, event = setup_data
        client.force_login(admin)
        change_url = reverse("admin:events_event_change", args=[event.pk])
        response = client.get(change_url)
        assert response.status_code == 200
        monitoring_url = reverse("admin:events_event_live_monitoring", args=[event.pk])
        assert monitoring_url in response.content.decode()

    def test_post_pause_action_creates_log_and_redirects(self, client, setup_data):
        admin, event = setup_data
        client.force_login(admin)
        url = reverse("admin:events_event_live_monitoring", args=[event.pk])

        response = client.post(url, {"action": "pause"}, follow=True)
        assert response.status_code == 200
        assert OperatorActionLog.objects.filter(
            event=event, action=OperatorActionType.PAUSE
        ).exists()

    def test_post_resume_action_creates_log_and_redirects(self, client, setup_data):
        admin, event = setup_data
        client.force_login(admin)
        url = reverse("admin:events_event_live_monitoring", args=[event.pk])

        # First pause to allow resume
        client.post(url, {"action": "pause"})
        response = client.post(url, {"action": "resume"}, follow=True)
        assert response.status_code == 200
        assert OperatorActionLog.objects.filter(
            event=event, action=OperatorActionType.RESUME
        ).exists()

    def test_post_extend_action_shifts_round_end(self, client, setup_data):
        admin, event = setup_data
        client.force_login(admin)
        url = reverse("admin:events_event_live_monitoring", args=[event.pk])

        r1 = Round.objects.get(event=event, number=1)
        initial_end = r1.ends_at

        response = client.post(url, {"action": "extend", "seconds": 120}, follow=True)
        assert response.status_code == 200

        r1.refresh_from_db()
        assert r1.ends_at == initial_end + timedelta(seconds=120)
        assert OperatorActionLog.objects.filter(
            event=event, action=OperatorActionType.EXTEND
        ).exists()
