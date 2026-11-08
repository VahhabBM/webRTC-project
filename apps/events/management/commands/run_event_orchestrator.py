from __future__ import annotations

import time
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.events.models import Event, Round
from apps.events.orchestrator import OrchestratorRealtime


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
            now = timezone.now()
            event_ids = Round.objects.filter(
                starts_at__lte=now + timedelta(seconds=20),
                ends_at__gte=now - timedelta(minutes=10),
            ).values_list("event_id", flat=True).distinct()

            for event in Event.objects.filter(pk__in=event_ids).iterator():
                try:
                    result = OrchestratorRealtime(event).dispatch(now=now)
                    if result["sent"]:
                        self.stdout.write(
                            f"Event {event.pk}: phase={result['phase']} "
                            f"round={result['round_number']} sent={result['sent']}"
                        )
                except Exception as exc:
                    self.stderr.write(
                        f"Event orchestrator dispatch failed for {event.pk}: {exc}"
                    )

            time.sleep(interval)
