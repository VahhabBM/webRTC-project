from datetime import timedelta

import pytest
from django.contrib.admin.sites import site
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.events.admin import ParticipantAdmin
from apps.events.models import Event, Participant, ParticipantStatus, Tag

User = get_user_model()


@pytest.mark.django_db
class TestParticipantAdmin:
    def test_admin_configuration_satisfies_requirements(self):
        admin_instance = site._registry[Participant]
        assert isinstance(admin_instance, ParticipantAdmin)

        for field in ["display_name", "email", "status", "display_tags", "created_at"]:
            assert field in admin_instance.list_display

        assert "display_name" in admin_instance.search_fields
        assert "email" in admin_instance.search_fields
        assert "status" in admin_instance.list_filter
        assert "tags" in admin_instance.list_filter
        assert "status" in admin_instance.list_editable
        assert admin_instance.list_per_page <= 100

    def test_participant_changelist_view_and_query_efficiency(self, client: Client):
        admin_user = User.objects.create_superuser(
            username="admin", email="admin@test.com", password="adminpassword"
        )
        client.force_login(admin_user)

        event = Event.objects.create(
            name="Test Event",
            num_rounds=3,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(seconds=30),
            start_time=timezone.now(),
        )
        tag1 = Tag.objects.create(name="Python")
        tag2 = Tag.objects.create(name="Django")

        for i in range(10):
            p = Participant.objects.create(
                event=event,
                display_name=f"User {i}",
                email=f"user{i}@test.com",
                status=ParticipantStatus.WAITING,
            )
            p.tags.add(tag1, tag2)

        response = client.get("/admin/events/participant/")
        assert response.status_code == 200
        assert "User 0" in response.content.decode("utf-8")
        assert "Python" in response.content.decode("utf-8")

        search_res = client.get("/admin/events/participant/?q=user3@test.com")
        assert search_res.status_code == 200
        assert "user3@test.com" in search_res.content.decode("utf-8")

        filter_res = client.get(
            f"/admin/events/participant/?status={ParticipantStatus.WAITING}"
        )
        assert filter_res.status_code == 200
