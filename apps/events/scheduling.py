"""Shared scheduling service: build match input and persist a generated schedule.

This module is the single authoritative place for DB-level schedule persistence,
shared by the management command (T-20) and the admin matching report (T-21).
The matching algorithm itself lives in matching.py and is never duplicated here.
"""

from __future__ import annotations

import hashlib
import uuid

from django.db import transaction

from apps.events.matching import MatchInput, MatchParticipant
from apps.events.models import Event, Pair, Round
from apps.events.scoring import build_participant_profile, build_scoring_weights


def build_match_input(event: Event) -> MatchInput:
    """Load participants from the DB and construct a MatchInput.

    Requires an active DB connection. Prefetches tags for efficiency.

    Raises
    ------
    ValueError
        If the event's scoring_weights configuration is invalid.
    """
    participants_qs = event.participants.all().prefetch_related("tags")
    participants = list(participants_qs)

    weights = build_scoring_weights(event)

    match_participants = tuple(
        sorted(
            (
                MatchParticipant(
                    pid=str(p.pk),
                    profile=build_participant_profile(p),
                )
                for p in participants
            ),
            key=lambda mp: mp.pid,
        )
    )

    return MatchInput(
        participants=match_participants,
        num_rounds=int(event.num_rounds),
        weights=weights,
    )


def persist_schedule(event: Event, schedule) -> None:
    """Atomically replace the event's existing schedule with the generated one.

    Deletes all existing rounds (and cascades to pairs), then creates new
    rounds and pairs in a single transaction.  If anything fails mid-write the
    transaction rolls back and the previous schedule remains intact.

    Parameters
    ----------
    event:
        The Event whose schedule is being replaced.
    schedule:
        A GeneratedSchedule returned by generate_schedule().
    """
    with transaction.atomic():
        event.rounds.all().delete()

        step = event.round_duration + event.break_duration
        round_objects: list[Round] = []
        for rnd in schedule.rounds:
            starts_at = event.start_time + (rnd.number - 1) * step
            ends_at = starts_at + event.round_duration
            round_objects.append(
                Round(
                    event=event,
                    number=rnd.number,
                    status="scheduled",
                    starts_at=starts_at,
                    ends_at=ends_at,
                )
            )

        Round.objects.bulk_create(round_objects)
        round_by_number = {r.number: r for r in round_objects}

        pair_objects: list[Pair] = []
        for rnd in schedule.rounds:
            round_obj = round_by_number[rnd.number]
            for sp in rnd.pairs:
                pid_a, pid_b = sp.pid_a, sp.pid_b
                if uuid.UUID(pid_a) > uuid.UUID(pid_b):
                    pid_a, pid_b = pid_b, pid_a

                room_id = hashlib.sha256(
                    f"{event.pk}:{round_obj.number}:{pid_a}:{pid_b}".encode()
                ).hexdigest()[:32]

                pair_objects.append(
                    Pair(
                        event=event,
                        round=round_obj,
                        participant_a_id=pid_a,
                        participant_b_id=pid_b,
                        room_id=room_id,
                    )
                )

        Pair.objects.bulk_create(pair_objects)
