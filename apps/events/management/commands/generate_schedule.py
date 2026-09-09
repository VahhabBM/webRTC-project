from __future__ import annotations

import hashlib
import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.events.matching import (
    MatchingError,
    MatchInput,
    MatchParticipant,
    generate_schedule,
    validate_schedule,
)
from apps.events.models import Event, Pair, Round
from apps.events.scoring import (
    build_participant_profile,
    build_scoring_weights,
)


class Command(BaseCommand):
    help = "Generate an R-round pairing schedule for an Event and persist it."

    def add_arguments(self, parser):
        parser.add_argument(
            "event_id",
            type=str,
            help="UUID of the Event to generate a schedule for.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Generate and validate without writing to the database.",
        )
        parser.add_argument(
            "--verbose",
            action="store_true",
            help="Print a per-round summary after generation.",
        )

    def handle(self, *args, **options):
        event_id_str: str = options["event_id"]
        dry_run: bool = bool(options["dry_run"])
        verbose: bool = bool(options["verbose"])

        try:
            event_uuid = uuid.UUID(event_id_str)
        except ValueError as exc:
            raise CommandError(f"Invalid UUID: {event_id_str}") from exc

        try:
            event = Event.objects.get(pk=event_uuid)
        except Event.DoesNotExist as exc:
            raise CommandError(f"Event {event_uuid} does not exist") from exc

        participants_qs = event.participants.all().prefetch_related("tags")
        participants = list(participants_qs)

        if len(participants) < 2:
            raise CommandError(
                f"Cannot generate schedule: event has fewer than 2 participants (got {len(participants)})."
            )

        try:
            weights = build_scoring_weights(event)
        except ValueError as exc:
            raise CommandError(
                f"Invalid scoring weights on event {event.pk}: {exc}"
            ) from exc

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

        match_input = MatchInput(
            participants=match_participants,
            num_rounds=int(event.num_rounds),
            weights=weights,
        )

        try:
            schedule = generate_schedule(match_input)
        except MatchingError as exc:
            raise CommandError(str(exc)) from exc

        pids = frozenset(mp.pid for mp in match_participants)
        violations = validate_schedule(schedule, pids, num_rounds=event.num_rounds)
        if violations:
            detail = "\n".join(f"- {v.kind}: {v.detail}" for v in violations)
            raise CommandError(f"Generated schedule is invalid:\n{detail}")

        if not dry_run:
            self._persist(event, schedule)

        total_pairs = sum(len(r.pairs) for r in schedule.rounds)
        prefix = "[DRY RUN] Would generate" if dry_run else "Generated"
        self.stdout.write(
            f"{prefix} {event.num_rounds} rounds for event '{event.name}' ({event.pk}) with {total_pairs} pairs."
        )

        if verbose:
            for rnd in schedule.rounds:
                total_score = sum(sp.score for sp in rnd.pairs)
                self.stdout.write(
                    f"Round {rnd.number}: pairs={len(rnd.pairs)} score={total_score:.6f} bye={rnd.unmatched_pid}"
                )

    @staticmethod
    def _persist(event: Event, schedule) -> None:
        with transaction.atomic():
            event.rounds.all().delete()

            round_objects: list[Round] = []
            step = event.round_duration + event.break_duration
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
