from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.core.cache import cache
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.events.auth import participant_session_cache_key
from apps.events.models import Event, EventStatus, Participant
from apps.registration.models import DeviceCheckLog


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
            event_end = event.start_time + (
                event.num_rounds * event.round_duration
                + max(event.num_rounds - 1, 0) * event.break_duration
            )
            if event_end > cutoff:
                continue

            participant_ids = list(
                Participant.objects.filter(event_id=event.pk).values_list(
                    "pk", flat=True
                )
            )
            device_logs = DeviceCheckLog.objects.filter(event_id=event.pk)
            device_log_count = device_logs.count()
            if not participant_ids and not device_log_count:
                continue

            eligible_events += 1
            eligible_participants += len(participant_ids)
            if dry_run:
                self.stdout.write(
                    f"Eligible event {event.pk}: {len(participant_ids)} participant(s), "
                    f"{device_log_count} device-check log(s)."
                )
                continue

            with transaction.atomic():
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
