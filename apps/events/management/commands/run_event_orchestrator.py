from __future__ import annotations

import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from apps.events.auth import scheduled_event_end
from apps.events.live_dispatch import dispatch_live_event
from apps.events.models import Event
from apps.events.scheduler import RoundScheduler


class Command(BaseCommand):
    help = "Dispatch scheduled event and round messages while events are running."

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval-seconds",
            type=int,
            default=2,
            help="Delay between scheduler checks (default: 2).",
        )

    def handle(self, *args, **options):
        interval = options["interval_seconds"]
        if interval < 1:
            self.stderr.write("--interval-seconds must be at least 1.")
            return

        self.stdout.write("Event orchestrator started.")
        while True:
            close_old_connections()
            now = timezone.now()
            events = Event.objects.filter(start_time__lte=now + timedelta(seconds=20)).exclude(
                status__in=("draft", "cancelled", "paused")
            )
            for event in events.iterator():
                if scheduled_event_end(event) < now - timedelta(minutes=10):
                    continue
                try:
                    if not event.rounds.exists():
                        RoundScheduler(event).persist_schedule()
                    result = dispatch_live_event(event, now=now)
                    if result["sent"] or result.get("allocated"):
                        self.stdout.write(
                            f"Event {event.pk}: phase={result['phase']} "
                            f"round={result['round_number']} sent={result['sent']}"
                        )
                except Exception as exc:
                    self.stderr.write(
                        f"Event orchestrator dispatch failed for {event.pk}: {exc}"
                    )

            time.sleep(interval)
