from datetime import timedelta

import pytest
from django.utils import timezone

from apps.events.auth import (
    ExpiredJoinToken,
    authenticate_join_token,
    extend_event_access_deadlines,
    issue_join_token,
    scheduled_event_end,
)
from apps.events.models import (
    Event,
    OperatorActionLog,
    Pair,
    Participant,
    ParticipantAccessToken,
)
from apps.events.orchestrator import OrchestratorRealtime
from apps.events.scheduler import RoundScheduler


@pytest.mark.django_db
def test_extension_preserves_current_and_emailed_links_but_not_revoked_links(monkeypatch):
    event = Event.objects.create(name="Extended event", status="scheduled", start_time=timezone.now(), num_rounds=1, round_duration=timedelta(minutes=2))
    round_obj = RoundScheduler(event).persist_schedule()[0]
    person = Participant.objects.create(event=event, display_name="A")
    old_link = issue_join_token(person)
    current_link = issue_join_token(person)
    before = scheduled_event_end(event)
    round_obj.ends_at += timedelta(minutes=2)
    round_obj.save(update_fields=["ends_at"])
    after = scheduled_event_end(event)
    assert after == round_obj.ends_at
    extend_event_access_deadlines(event, before, after)
    monkeypatch.setattr(timezone, "now", lambda: before + timedelta(seconds=5))
    assert authenticate_join_token(old_link).pk == person.pk
    assert authenticate_join_token(current_link).pk == person.pk
    ParticipantAccessToken.objects.filter(participant=person).update(expires_at=before - timedelta(seconds=1))
    extend_event_access_deadlines(event, before, after + timedelta(minutes=1))
    with pytest.raises(ExpiredJoinToken):
        authenticate_join_token(old_link)


@pytest.mark.django_db
def test_paused_room_replays_frozen_timer_and_preserves_whole_event_link():
    now = timezone.now()
    event = Event.objects.create(name="Frozen event", status="scheduled", start_time=now - timedelta(minutes=3), num_rounds=1, round_duration=timedelta(minutes=1))
    round_obj = RoundScheduler(event).persist_schedule()[0]
    people = [Participant.objects.create(event=event, display_name=name) for name in ("A", "B")]
    people.sort(key=lambda p: p.pk)
    link = issue_join_token(people[0])
    Pair.objects.create(event=event, round=round_obj, participant_a=people[0], participant_b=people[1], room_id="frozen-pair", live_allocated=True)
    event.status = "paused"
    event.save(update_fields=["status"])
    OperatorActionLog.objects.create(event=event, round=round_obj, action="pause", details={"remaining_seconds": 20, "paused_at": (now - timedelta(minutes=2)).isoformat()})
    assert authenticate_join_token(link).pk == people[0].pk
    snapshot = OrchestratorRealtime(event).reconnect_snapshot_messages(people[0], now=now)
    assert [message["type"] for message in snapshot] == ["server.pairing", "server.round_start", "operator.pause"]
    assert snapshot[0]["payload"]["round_end_ts"] > int(now.timestamp() * 1000)
    assert snapshot[-1]["payload"]["remaining_seconds"] == 20
