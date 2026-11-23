from datetime import timedelta

import pytest
from django.utils import timezone

from apps.events.live_dispatch import dispatch_live_event
from apps.events.models import Event, Participant
from apps.events.scheduler import RoundScheduler


@pytest.mark.django_db
def test_late_ready_arrival_is_paired_after_round_broadcast(monkeypatch):
    now = timezone.now()
    event = Event.objects.create(
        name="Late arrival rehearsal", start_time=now - timedelta(seconds=10),
        num_rounds=1, round_duration=timedelta(minutes=5), status="scheduled",
    )
    RoundScheduler(event).persist_schedule()
    man = Participant.objects.create(event=event, display_name="A", gender="Male", country="DE")
    woman = Participant.objects.create(event=event, display_name="B", gender="Female", country="DE")
    online = {str(man.pk)}
    monkeypatch.setattr("apps.events.live_matching.get_online_participant_ids", lambda *_: set(online))
    sent = []
    monkeypatch.setattr("apps.events.orchestrator.OrchestratorRealtime._publish", lambda self, group, msg: sent.append((group, msg)) or True)
    first = dispatch_live_event(event, now=now)
    assert first["allocated"] == 0
    waiting = [message for _, message in sent if message["type"] == "server.waiting"]
    assert waiting[0]["payload"]["reason"] == "waiting_for_partner"
    assert not event.pairs.exists()

    sent.clear()
    online.add(str(woman.pk))
    second = dispatch_live_event(event, now=now + timedelta(seconds=2))
    assert second["allocated"] == 1
    assert len([m for _, m in sent if m["type"] == "server.pairing"]) == 2
    assert len([m for _, m in sent if m["type"] == "server.round_start"]) == 2
    sent.clear()
    third = dispatch_live_event(event, now=now + timedelta(seconds=4))
    assert third["allocated"] == 0
    assert not sent  # an unchanged tick never restarts an existing call


@pytest.mark.django_db
def test_offline_static_assignments_are_never_broadcast(monkeypatch):
    from apps.events.models import Pair

    now = timezone.now()
    event = Event.objects.create(name="Offline allocation", start_time=now, num_rounds=1)
    round_obj = RoundScheduler(event).persist_schedule()[0]
    people = [Participant.objects.create(event=event, display_name=name) for name in ("A", "B")]
    people.sort(key=lambda p: p.pk)
    Pair.objects.create(event=event, round=round_obj, participant_a=people[0], participant_b=people[1], room_id="static-room")
    monkeypatch.setattr("apps.events.live_matching.get_online_participant_ids", lambda *_: set())
    sent = []
    monkeypatch.setattr("apps.events.orchestrator.OrchestratorRealtime._publish", lambda self, group, msg: sent.append(msg) or True)
    dispatch_live_event(event, now=now)
    assert not event.pairs.exists()
    assert not any(msg["type"] in ("server.pairing", "server.round_start") for msg in sent)


@pytest.mark.django_db
def test_live_room_retries_only_failed_lifecycle_recipient(monkeypatch):
    from apps.events.orchestrator import participant_channel_group

    now = timezone.now()
    event = Event.objects.create(
        name="Retry replacement delivery", start_time=now - timedelta(seconds=10),
        num_rounds=1, round_duration=timedelta(minutes=5), status="scheduled",
    )
    RoundScheduler(event).persist_schedule()
    man = Participant.objects.create(event=event, display_name="A", gender="Male", country="DE")
    woman = Participant.objects.create(event=event, display_name="B", gender="Female", country="DE")
    online = {str(man.pk), str(woman.pk)}
    monkeypatch.setattr("apps.events.live_matching.get_online_participant_ids", lambda *_: set(online))

    failed_group = participant_channel_group(man.pk)
    failed_types = {"server.pairing", "server.round_start"}
    sent = []

    def publish(self, group, message):
        message_type = message["type"]
        if group == failed_group and message_type in failed_types:
            failed_types.remove(message_type)
            return False
        sent.append((group, message))
        return True

    monkeypatch.setattr("apps.events.orchestrator.OrchestratorRealtime._publish", publish)
    first = dispatch_live_event(event, now=now)
    assert first["allocated"] == 1
    assert not [m for g, m in sent if g == failed_group]

    second = dispatch_live_event(event, now=now + timedelta(seconds=2))
    assert second["allocated"] == 0

    for message_type in ("server.pairing", "server.round_start"):
        for participant in (man, woman):
            recipient_group = participant_channel_group(participant.pk)
            assert sum(
                group == recipient_group and message["type"] == message_type
                for group, message in sent
            ) == 1
