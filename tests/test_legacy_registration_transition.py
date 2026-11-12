from datetime import timedelta
from importlib import import_module

import pytest
from django.apps import apps
from django.db import transaction
from django.utils import timezone

from apps.events.models import Event, Participant


@pytest.mark.django_db
def test_pending_transition_respects_capacity_and_preserves_email_truth(settings):
    settings.EVENT_REGISTRATION_CAPACITY = 1
    future = Event.objects.create(name="Future transition", status="scheduled", start_time=timezone.now() + timedelta(days=1))
    past = Event.objects.create(name="Past unchanged", status="scheduled", start_time=timezone.now() - timedelta(days=3))
    first = Participant.objects.create(event=future, display_name="A", registration_status="pending")
    second = Participant.objects.create(event=future, display_name="B", registration_status="pending")
    historical = Participant.objects.create(event=past, display_name="C", registration_status="pending")
    migrate = import_module("apps.events.migrations.0026_complete_legacy_pending_registrations").complete_pending_registrations
    with transaction.atomic():
        migrate(apps, None)
    for participant in (first, second, historical):
        participant.refresh_from_db()
        assert participant.email_verified_at is None
        assert participant.joined_at is None
    assert first.registration_status == "confirmed"
    assert second.registration_status == "waitlisted"
    assert historical.registration_status == "pending"
    with transaction.atomic():
        migrate(apps, None)
    assert Participant.objects.filter(event=future, registration_status="confirmed").count() == 1
