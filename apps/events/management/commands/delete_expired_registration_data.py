from __future__ import annotations

import time
from collections import Counter
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.events.auth import participant_session_cache_key, scheduled_event_end
from apps.events.models import (
    Event,
    EventRegistrationStatsSnapshot,
    EventStatus,
    Participant,
)
from apps.registration.models import DeviceCheckLog

_MIN_AGGREGATE_CELL_SIZE = 5
_OTHER_AGGREGATE_BUCKET = "__other__"


def _aggregate_counts(values):
    counts = Counter(values)
    retained = {
        value: count
        for value, count in counts.items()
        if count >= _MIN_AGGREGATE_CELL_SIZE
    }
    small_groups = sum(
        count for count in counts.values() if count < _MIN_AGGREGATE_CELL_SIZE
    )
    if small_groups:
        retained[_OTHER_AGGREGATE_BUCKET] = small_groups
    return dict(sorted(retained.items()))


class Command(BaseCommand):
    help = "Delete participant registration data two days after each event ends."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Keep checking for expired registration data at the configured interval.",
        )
        parser.add_argument(
            "--interval-seconds",
            type=int,
            default=3600,
            help="Delay between checks when --loop is enabled (default: 3600).",
        )
        parser.add_argument(
            "--confirm-production",
            action="store_true",
            help="Confirm deletion of expired registration data in production.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report eligible records without deleting anything.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["confirm_production"]:
            raise CommandError(
                "Refusing production deletion without --confirm-production."
            )

        interval = options["interval_seconds"]
        if options["loop"] and interval < 60:
            raise CommandError("--interval-seconds must be at least 60.")
        if options["loop"] and options["dry_run"]:
            raise CommandError("--dry-run cannot be combined with --loop.")

        while True:
            self.delete_expired_data(dry_run=options["dry_run"])
            if not options["loop"]:
                return
            time.sleep(interval)

    def delete_expired_data(self, *, dry_run: bool) -> None:
        cutoff = timezone.now() - timedelta(days=2)
        eligible_events = 0
        eligible_participants = 0
        deleted_participants = 0
        deleted_device_logs = 0

        excluded_statuses = (
            EventStatus.DRAFT,
            EventStatus.PAUSED,
        )
        for event in Event.objects.exclude(status__in=excluded_statuses).iterator():
            if scheduled_event_end(event) > cutoff:
                continue

            if dry_run:
                participant_count = Participant.objects.filter(
                    event_id=event.pk
                ).count()
                device_log_count = DeviceCheckLog.objects.filter(
                    event_id=event.pk
                ).count()
                if not participant_count and not device_log_count:
                    continue
                eligible_events += 1
                eligible_participants += participant_count
                self.stdout.write(
                    f"Eligible event {event.pk}: {participant_count} participant(s), "
                    f"{device_log_count} device-check log(s)."
                )
                continue

            with transaction.atomic():
                locked_event = Event.objects.select_for_update().get(pk=event.pk)
                if scheduled_event_end(locked_event) > cutoff:
                    continue

                participant_rows = list(
                    Participant.objects.filter(event_id=locked_event.pk).values_list(
                        "pk", "gender", "country"
                    )
                )
                participant_ids = [row[0] for row in participant_rows]
                device_logs = DeviceCheckLog.objects.filter(event_id=locked_event.pk)
                device_log_count = device_logs.count()
                if not participant_ids and not device_log_count:
                    continue

                eligible_events += 1
                eligible_participants += len(participant_ids)
                EventRegistrationStatsSnapshot.objects.get_or_create(
                    event=locked_event,
                    defaults={
                        "total_registrations": len(participant_rows),
                        "gender_counts": _aggregate_counts(
                            gender or "Not provided"
                            for _, gender, _ in participant_rows
                        ),
                        "country_counts": _aggregate_counts(
                            country or "" for _, _, country in participant_rows
                        ),
                    },
                )
                deleted_device_logs += device_logs.delete()[0]
                deleted_count, _ = Participant.objects.filter(
                    pk__in=participant_ids
                ).delete()
                deleted_participants += deleted_count

            cache.delete_many(
                participant_session_cache_key(participant_id)
                for participant_id in participant_ids
            )

        if dry_run:
            self.stdout.write(
                f"Dry run: {eligible_events} event(s), {eligible_participants} "
                "participant(s) and their related registration records are eligible."
            )
        elif eligible_events or deleted_device_logs:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Removed expired data for {eligible_events} event(s): "
                    f"{deleted_participants} participant-related record(s) and "
                    f"{deleted_device_logs} device-check log(s)."
                )
            )
