from __future__ import annotations

import os
import random
from datetime import datetime, timedelta
from uuid import uuid4

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.events.models import (
    Event,
    EventStatus,
    Participant,
    ParticipantStatus,
    ParticipantTag,
    Round,
    Tag,
)

SAMPLE_TAGS = (
    ("Technology", "common"),
    ("Entrepreneurship", "common"),
    ("Design", "common"),
    ("Marketing", "common"),
    ("Leadership", "common"),
    ("Product", "medium"),
    ("Data Science", "medium"),
    ("Startups", "medium"),
    ("Remote Work", "medium"),
    ("Climate", "medium"),
    ("WebRTC", "rare"),
    ("Rust", "rare"),
    ("Accessibility", "rare"),
    ("Quantum Computing", "rare"),
    ("Biohacking", "rare"),
    ("Urban Gardening", "rare"),
)

FIRST_NAMES = (
    "Alex",
    "Blair",
    "Casey",
    "Drew",
    "Elliot",
    "Frankie",
    "Jordan",
    "Kai",
    "Morgan",
    "Riley",
    "Sam",
    "Taylor",
)
LAST_NAMES = (
    "Adams",
    "Baker",
    "Carter",
    "Diaz",
    "Evans",
    "Foster",
    "Green",
    "Hughes",
    "Irwin",
    "Jones",
    "Khan",
    "Lee",
)


class Command(BaseCommand):
    help = (
        "Generate a synthetic Event, rounds, participants, and realistic sample tags. "
        "Production settings require --confirm-production."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--participants",
            type=int,
            default=100,
            help="Number of synthetic participants to create (default: 100).",
        )
        parser.add_argument(
            "--event-name", default=None, help="Base name for the Event."
        )
        parser.add_argument(
            "--description", default="Synthetic data for pairing-engine development."
        )
        parser.add_argument("--num-rounds", type=int, default=3)
        parser.add_argument(
            "--round-duration", type=int, default=5, help="Round duration in minutes."
        )
        parser.add_argument(
            "--break-duration", type=int, default=1, help="Break duration in minutes."
        )
        parser.add_argument(
            "--start-time",
            type=str,
            default=None,
            help="ISO-8601 start time (default: one hour from now).",
        )
        parser.add_argument(
            "--seed", type=int, default=None, help="Random seed for reproducible data."
        )
        parser.add_argument(
            "--confirm-production",
            action="store_true",
            help="Required when DJANGO_SETTINGS_MODULE points to config.settings.production.",
        )

    def handle(self, *args, **options):
        if self._is_production() and not options["confirm_production"]:
            raise CommandError(
                "Refusing to seed production: pass --confirm-production explicitly."
            )
        count = options["participants"]
        if count < 1:
            raise CommandError("--participants must be at least 1.")
        if options["num_rounds"] < 1:
            raise CommandError("--num-rounds must be at least 1.")
        if options["round_duration"] < 1 or options["break_duration"] < 0:
            raise CommandError(
                "Durations must be positive (break duration may be zero)."
            )

        rng = random.Random(options["seed"])
        start_time = self._parse_start_time(options["start_time"])
        event_name = self._unique_event_name(options["event_name"])

        with transaction.atomic():
            tags = self._get_sample_tags()
            event = Event.objects.create(
                name=event_name,
                description=options["description"],
                status=EventStatus.DRAFT,
                num_rounds=options["num_rounds"],
                round_duration=timedelta(minutes=options["round_duration"]),
                break_duration=timedelta(minutes=options["break_duration"]),
                start_time=start_time,
            )
            rounds = [
                Round(
                    event=event,
                    number=number,
                    starts_at=start_time
                    + (number - 1) * (event.round_duration + event.break_duration),
                    ends_at=start_time
                    + (number - 1) * (event.round_duration + event.break_duration)
                    + event.round_duration,
                )
                for number in range(1, event.num_rounds + 1)
            ]
            Round.objects.bulk_create(rounds)
            participants = []
            for number in range(1, count + 1):
                display_name = self._participant_name(number, rng)
                participant = Participant(
                    event=event,
                    display_name=display_name,
                    email=f"participant-{number}@seed.invalid",
                    status=ParticipantStatus.WAITING,
                    join_token_hash="!",
                )
                participant.set_join_token(f"seed-{event.id}-{number}-{uuid4().hex}")
                participants.append(participant)
            Participant.objects.bulk_create(participants)
            participant_tags = []
            for participant in participants:
                assigned = self._select_tags(tags, rng)
                participant_tags.extend(
                    ParticipantTag(participant=participant, tag=tag) for tag in assigned
                )
            ParticipantTag.objects.bulk_create(participant_tags, ignore_conflicts=True)

        self.stdout.write(
            self.style.SUCCESS(
                f"Created Event '{event.name}' ({event.pk}) with "
                f"{count} participants, {len(tags)} tags, and {len(rounds)} rounds."
            )
        )

    @staticmethod
    def _is_production() -> bool:
        module = os.environ.get("DJANGO_SETTINGS_MODULE", "")
        return (
            module == "config.settings.production"
            or getattr(settings, "ENVIRONMENT", "") == "production"
        )

    @staticmethod
    def _parse_start_time(value: str | None):
        if not value:
            return timezone.now() + timedelta(hours=1)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise CommandError(
                "--start-time must be a valid ISO-8601 datetime."
            ) from exc
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed

    @staticmethod
    def _unique_event_name(base_name: str | None) -> str:
        base = base_name or "Synthetic pairing test event"
        suffix = timezone.now().strftime("%Y%m%d-%H%M%S")
        name = f"{base} ({suffix}-{uuid4().hex[:6]})"
        return name[:200]

    @staticmethod
    def _get_sample_tags() -> list[Tag]:
        existing = {
            tag.name: tag
            for tag in Tag.objects.filter(name__in=[name for name, _ in SAMPLE_TAGS])
        }
        tags = []
        for name, _ in SAMPLE_TAGS:
            tags.append(existing.get(name) or Tag.objects.create(name=name))
        return tags

    @staticmethod
    def _participant_name(number: int, rng: random.Random) -> str:
        return f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)} {number:04d}"

    @staticmethod
    def _select_tags(tags: list[Tag], rng: random.Random) -> list[Tag]:
        weights = [
            0.22 if category == "common" else 0.07 if category == "medium" else 0.02
            for _, category in SAMPLE_TAGS
        ]
        selected = [tag for tag, weight in zip(tags, weights) if rng.random() < weight]
        if not selected:
            selected = [tags[rng.randrange(5)]]
        if rng.random() < 0.12:
            rare = tags[11 + rng.randrange(5)]
            if rare not in selected:
                selected.append(rare)
        return selected
