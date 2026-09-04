import json
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.events.models import Event
from apps.registration.models import DeviceCheckLog


@pytest.mark.django_db
class TestDeviceCheckFlow:
    def test_device_check_page_renders_html(self, client: Client):
        event = Event.objects.create(
            name="Global Tech Summit 2026",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now(),
        )

        response = client.get(f"/registration/{event.id}/device-check/")
        assert response.status_code == 200
        content = response.content.decode("utf-8")
        assert "Audio & Video Readiness" in content
        assert "camera-video" in content
        assert "playsinline" in content
        assert "mic-level-fill" in content
        assert "iab-warning" in content

    def test_device_check_report_api_records_log(self, client: Client):
        event = Event.objects.create(
            name="Robotics Networking",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now(),
        )

        payload = {
            "camera_working": True,
            "mic_working": True,
            "error_type": "",
            "is_in_app_browser": False,
        }

        response = client.post(
            f"/registration/{event.id}/device-check/report/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_USER_AGENT="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
        )

        assert response.status_code == 201
        assert DeviceCheckLog.objects.filter(event=event).count() == 1
        log = DeviceCheckLog.objects.first()
        assert log.camera_working is True
        assert log.mic_working is True
        assert "iPhone" in log.user_agent

    def test_device_check_report_api_records_permission_denied(self, client: Client):
        payload = {
            "camera_working": False,
            "mic_working": False,
            "error_type": "not_allowed",
            "is_in_app_browser": True,
        }

        response = client.post(
            "/registration/device-check/report/",
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_USER_AGENT="Instagram 300.0",
        )

        assert response.status_code == 201
        log = DeviceCheckLog.objects.first()
        assert log.error_type == "not_allowed"
        assert log.is_in_app_browser is True
