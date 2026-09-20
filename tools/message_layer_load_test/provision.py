"""Create a dedicated T-44 event, participants, and two pairing rounds.

Uses existing T-08 ``issue_join_token`` and T-20 pair rows. Raw join tokens
are returned in memory only and must not be written to the result file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.events.auth import issue_join_token
from apps.events.models import Event, EventStatus, Pair, Participant, Round

from . import DEFAULT_CLIENT_COUNT, MAX_CLIENT_COUNT, MIN_CLIENT_COUNT


class ProvisionError(ValueError):
    """The load-test fixture cannot be created with the requested shape."""


@dataclass
class SyntheticIdentity:
    index: int
    participant_id: str
    display_name: str


@dataclass
class ProvisionedLoadTest:
    event_id: str
    event_name: str
    round_numbers: tuple[int, int]
    clients: list[SyntheticIdentity]
    pair_counts: dict[int, int]
    _join_tokens: list[str] = field(repr=False, default_factory=list)

    def token_for(self, index: int) -> str:
        return self._join_tokens[index]

    def clear_secrets(self) -> None:
        self._join_tokens = [""] * len(self._join_tokens)


def validate_client_count(count: int) -> int:
    if count % 2:
        raise ProvisionError(
            "Client count must be even so every synthetic client is paired."
        )
    if count < MIN_CLIENT_COUNT:
        raise ProvisionError(
            f"Client count must be at least {MIN_CLIENT_COUNT} so round 2 can switch partners "
            "without repeating a T-20 pair."
        )
    if count > MAX_CLIENT_COUNT:
        raise ProvisionError(
            f"Client count {count} exceeds the T-44 tool cap of {MAX_CLIENT_COUNT}. "
            "900-client execution is out of scope for this task."
        )
    return count


def rotated_pairs(
    participants: list[Participant], offset: int
) -> list[tuple[Participant, Participant]]:
    """Pair adjacent participants after rotating by ``offset``.

    Offset 0 and 1 yield distinct unordered pairs, satisfying the
    no-repeat-partner constraint for two rounds.
    """
    n = len(participants)
    rotated = participants[offset:] + participants[:offset]
    pairs: list[tuple[Participant, Participant]] = []
    for i in range(0, n, 2):
        left, right = rotated[i], rotated[i + 1]
        if left.pk > right.pk:
            left, right = right, left
        pairs.append((left, right))
    return pairs


def _is_production() -> bool:
    module = getattr(settings, "SETTINGS_MODULE", "") or ""
    env_module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
    return (
        env_module == "config.settings.production"
        or module == "config.settings.production"
        or getattr(settings, "ENVIRONMENT", "") == "production"
    )


def refuse_production() -> None:
    if _is_production():
        raise ProvisionError(
            "Refusing to run the message-layer load test against production. "
            "Use local or staging settings only."
        )


def provision_load_test_event(
    client_count: int = DEFAULT_CLIENT_COUNT,
    *,
    event_name: str | None = None,
) -> ProvisionedLoadTest:
    """Insert a throwaway event with two rounds and rotated partners."""
    refuse_production()
    count = validate_client_count(client_count)
    now = timezone.now()
    name = (
        event_name or f"T-44 message-layer load test ({now.strftime('%Y%m%d-%H%M%S')})"
    )
    round_duration = timedelta(minutes=10)
    break_duration = timedelta(seconds=0)

    with transaction.atomic():
        event = Event.objects.create(
            name=name[:200],
            description="Synthetic T-44 load-test event. Not a production cohort.",
            status=EventStatus.RUNNING,
            num_rounds=2,
            round_duration=round_duration,
            break_duration=break_duration,
            start_time=now,
        )
        participants = [
            Participant(
                event=event,
                display_name=f"T44 Client {index:03d}",
                email=f"t44-{index:03d}@loadtest.invalid",
                join_token_hash="!",
            )
            for index in range(count)
        ]
        Participant.objects.bulk_create(participants)
        participants = list(
            Participant.objects.filter(event=event).order_by("display_name")
        )
        rounds = [
            Round(
                event=event,
                number=number,
                starts_at=now + (number - 1) * round_duration,
                ends_at=now + number * round_duration,
            )
            for number in (1, 2)
        ]
        Round.objects.bulk_create(rounds)
        rounds = list(Round.objects.filter(event=event).order_by("number"))
        pair_rows: list[Pair] = []
        pair_counts: dict[int, int] = {}
        for round_obj, offset in zip(rounds, (0, 1), strict=True):
            pairs = rotated_pairs(participants, offset)
            pair_counts[round_obj.number] = len(pairs)
            for index, (left, right) in enumerate(pairs):
                pair_rows.append(
                    Pair(
                        event=event,
                        round=round_obj,
                        participant_a=left,
                        participant_b=right,
                        room_id=f"t44-{event.pk.hex[:8]}-r{round_obj.number}-{index:03d}",
                    )
                )
        Pair.objects.bulk_create(pair_rows)

        identities: list[SyntheticIdentity] = []
        tokens: list[str] = []
        for index, participant in enumerate(participants):
            tokens.append(issue_join_token(participant))
            identities.append(
                SyntheticIdentity(
                    index=index,
                    participant_id=str(participant.pk),
                    display_name=participant.display_name,
                )
            )

    return ProvisionedLoadTest(
        event_id=str(event.pk),
        event_name=event.name,
        round_numbers=(1, 2),
        clients=identities,
        pair_counts=pair_counts,
        _join_tokens=tokens,
    )


def delete_load_test_event(event_id: str) -> None:
    Event.objects.filter(pk=event_id).delete()
