import pytest
from datetime import timedelta
from django.utils import timezone
from apps.events.models import Event
from apps.events.operator import OperatorService


@pytest.mark.django_db
def test_operator_live_metrics_and_alerts():
    event = Event.objects.create(
        name="Test Event T-43",
        start_time=timezone.now(),
        status="active",
        num_rounds=3,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(minutes=1),
    )

    event.active_connections_count = 20
    event.active_rooms_count = 10
    event.relay_path_share_percentage = 40.0
    event.fallback_upgrades_count = 3
    event.message_latency_ms = 50.0
    event.drop_rate_one_minute = 6.0
    event.save()

    service = OperatorService(event, is_leader=True)
    metrics = service.get_live_metrics()

    assert metrics["active_connections_count"] == 20
    assert metrics["active_rooms_count"] == 10
    assert metrics["relay_path_share_percentage"] == 40.0
    assert metrics["has_critical_alerts"] is True
    assert len(metrics["alerts"]) == 3