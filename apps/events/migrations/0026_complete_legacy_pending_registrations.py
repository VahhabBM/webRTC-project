from datetime import timedelta

from django.conf import settings
from django.db import migrations
from django.utils import timezone


def complete_pending_registrations(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    Participant = apps.get_model("events", "Participant")
    now = timezone.now()
    capacity = max(0, int(getattr(settings, "EVENT_REGISTRATION_CAPACITY", 200)))
    events = Event.objects.exclude(status__in=("cancelled", "completed", "draft"))
    for event in events.select_for_update().iterator():
        end = event.start_time + timedelta(seconds=event.intro_video_duration_seconds) + (
            event.num_rounds * event.round_duration
            + max(0, event.num_rounds - 1) * event.break_duration
        )
        if end <= now:
            continue
        occupied = Participant.objects.filter(event=event, registration_status="confirmed").count()
        pending = Participant.objects.filter(event=event, registration_status="pending").order_by("created_at", "pk")
        for participant in pending.iterator():
            status = "confirmed" if occupied < capacity else "waitlisted"
            Participant.objects.filter(pk=participant.pk, registration_status="pending").update(
                registration_status=status, reservation_expires_at=None,
            )
            if status == "confirmed":
                occupied += 1


class Migration(migrations.Migration):
    dependencies = [("events", "0025_allow_shared_nicknames")]
    operations = [migrations.RunPython(complete_pending_registrations, migrations.RunPython.noop)]
