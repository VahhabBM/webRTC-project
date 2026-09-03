from datetime import timedelta

import pytest
from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.contrib.messages.storage.fallback import FallbackStorage
from django.test import Client, RequestFactory
from django.utils import timezone

from apps.events.admin import EventAdmin, TagAdmin
from apps.events.models import (
    Event,
    EventStatus,
    Participant,
    ParticipantStatus,
    Tag,
)

User = get_user_model()


@pytest.mark.django_db
class TestEventAndTagAdmin:
    def test_event_admin_configuration(self):
        admin_instance = site._registry[Event]
        assert isinstance(admin_instance, EventAdmin)

        for field in [
            "name",
            "status",
            "start_time",
            "num_rounds",
            "round_duration",
            "break_duration",
        ]:
            assert field in admin_instance.list_display

        assert "status" in admin_instance.list_editable
        assert "status" in admin_instance.list_filter
        assert "name" in admin_instance.search_fields

    def test_tag_admin_prevents_deletion_of_used_tag(self):
        admin_instance = site._registry[Tag]
        assert isinstance(admin_instance, TagAdmin)

        admin_user = User.objects.create_superuser(
            username="admin_tag",
            email="admintag@test.com",
            password="adminpassword",
        )

        event = Event.objects.create(
            name="Tech Event",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=timezone.now(),
        )
        used_tag = Tag.objects.create(name="Backend")
        unused_tag = Tag.objects.create(name="Design")

        participant = Participant.objects.create(
            event=event,
            display_name="Sara",
            email="sara@example.com",
            status=ParticipantStatus.WAITING,
        )
        participant.tags.add(used_tag)

        request = RequestFactory().get("/admin/events/tag/")
        request.user = admin_user
        request.session = {}
        request._messages = FallbackStorage(request)

        assert admin_instance.has_delete_permission(request, obj=used_tag) is False
        assert admin_instance.has_delete_permission(request, obj=unused_tag) is True

        admin_instance.delete_model(request, used_tag)
        assert Tag.objects.filter(id=used_tag.id).exists()

        admin_instance.delete_model(request, unused_tag)
        assert not Tag.objects.filter(id=unused_tag.id).exists()

    def test_event_changelist_and_creation(self, client: Client):
        admin_user = User.objects.create_superuser(
            username="admin_event",
            email="adminevent@test.com",
            password="adminpassword",
        )
        client.force_login(admin_user)

        Event.objects.create(
            name="Conf 2026",
            status=EventStatus.DRAFT,
            num_rounds=4,
            round_duration=timedelta(minutes=6),
            break_duration=timedelta(minutes=2),
            start_time=timezone.now(),
        )

        response = client.get("/admin/events/event/")
        assert response.status_code == 200
        assert "Conf 2026" in response.content.decode("utf-8")
