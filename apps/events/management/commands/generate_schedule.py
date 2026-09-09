from __future__ import annotations

import uuid

from django.core.management.base import BaseCommand, CommandError

from apps.events.matching import (
    MatchingError,
    generate_schedule,
    validate_schedule,
)
from apps.events.models import Event
from apps.events.scheduling import build_match_input, persist_schedule


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

        try:
            match_input = build_match_input(event)
        except ValueError as exc:
            raise CommandError(
                f"Invalid scoring weights on event {event.pk}: {exc}"
            ) from exc

        if len(match_input.participants) < 2:
            raise CommandError(
                f"Cannot generate schedule: event has fewer than 2 participants "
                f"(got {len(match_input.participants)})."
            )

        try:
            schedule = generate_schedule(match_input)
        except MatchingError as exc:
            raise CommandError(str(exc)) from exc

        pids = frozenset(mp.pid for mp in match_input.participants)
        violations = validate_schedule(schedule, pids, num_rounds=event.num_rounds)
        if violations:
            detail = "\n".join(f"- {v.kind}: {v.detail}" for v in violations)
            raise CommandError(f"Generated schedule is invalid:\n{detail}")

        if not dry_run:
            persist_schedule(event, schedule)

        total_pairs = sum(len(r.pairs) for r in schedule.rounds)
        prefix = "[DRY RUN] Would generate" if dry_run else "Generated"
        self.stdout.write(
            f"{prefix} {event.num_rounds} rounds for event '{event.name}' "
            f"({event.pk}) with {total_pairs} pairs."
        )

        if verbose:
            for rnd in schedule.rounds:
                total_score = sum(sp.score for sp in rnd.pairs)
                self.stdout.write(
                    f"Round {rnd.number}: pairs={len(rnd.pairs)} "
                    f"score={total_score:.6f} bye={rnd.unmatched_pid}"
                )
