"""Multi-round domain simulation; media outcomes here are simulated explicitly."""

from collections import Counter
from datetime import timedelta

import pytest
from django.utils import timezone

from apps.events.live_matching import reconcile_live_round
from apps.events.models import Event, Pair, Participant
from apps.events.scheduler import RoundScheduler


@pytest.mark.django_db
@pytest.mark.parametrize("men_count", [20, 30])
def test_eight_rounds_distribute_scarce_dates_fairly_without_repeating(men_count, monkeypatch):
    event = Event.objects.create(
        name="Eight-round attendance simulation", start_time=timezone.now(),
        status="scheduled", num_rounds=8, male_date_limit=4, female_date_limit=8,
        round_duration=timedelta(minutes=4), break_duration=timedelta(seconds=30),
    )
    men = [Participant.objects.create(event=event, display_name=f"M{i}", gender="Male", want_to_date="Female", country="DE", city="Berlin") for i in range(men_count)]
    women = [Participant.objects.create(event=event, display_name=f"W{i}", gender="Female", want_to_date="Male", country="DE", city="Berlin") for i in range(10)]
    online = {str(p.pk) for p in men + women}
    monkeypatch.setattr("apps.events.live_matching.get_online_participant_ids", lambda *_: set(online))
    rounds = RoundScheduler(event).persist_schedule()
    seen = set()
    for round_obj in rounds:
        outcome = reconcile_live_round(event, round_obj, now=round_obj.starts_at + timedelta(seconds=1))
        assert len(outcome["new_pairs"]) == 10
        occupants = []
        for pair in outcome["new_pairs"]:
            key = (pair.participant_a_id, pair.participant_b_id)
            assert key not in seen
            seen.add(key)
            occupants.extend(key)
            # Simulate the browser's successful bidirectional-media report.
            Pair.objects.filter(pk=pair.pk).update(
                connection_type_a="relay", connection_type_b="relay",
                media_received_a=True, media_received_b=True,
            )
        assert len(occupants) == len(set(occupants))
        unchanged = reconcile_live_round(event, round_obj, now=round_obj.starts_at + timedelta(seconds=5))
        assert not unchanged["new_pairs"]
        assert not unchanged["cancelled_participant_ids"]
    counts = Counter()
    for pair in event.pairs.all():
        counts.update((pair.participant_a_id, pair.participant_b_id))
    assert all(counts[p.pk] == 8 for p in women)
    male_counts = [counts[p.pk] for p in men]
    assert 0 < min(male_counts) <= max(male_counts) <= 4
    assert max(male_counts) - min(male_counts) <= 1
    if men_count == 20:
        assert set(male_counts) == {4}
