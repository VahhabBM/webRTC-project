from __future__ import annotations

import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.events.email_notifications import send_event_access_email_once
from apps.events.models import Event, EventStatus, Participant


class Command(BaseCommand):
    help = "Send room access links to verified participants within 24 hours of an event."

    def add_arguments(self, parser):
        parser.add_argument(
            "--loop",
            action="store_true",
            help="Keep checking for due emails at the configured interval.",
        )
        parser.add_argument(
            "--interval-seconds",
            type=int,
            default=300,
            help="Delay between checks when --loop is enabled (default: 300).",
        )
        parser.add_argument(
            "--confirm-production",
            action="store_true",
            help="Confirm sending real emails from production.",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["confirm_production"]:
            raise CommandError(
                "Refusing to send production emails without --confirm-production."
            )

        interval = options["interval_seconds"]
        if options["loop"] and interval < 30:
            raise CommandError("--interval-seconds must be at least 30.")
        self.loop_mode = options["loop"]

        while True:
            try:
                self.send_due_emails()
            except Exception as exc:
                if not options["loop"]:
                    raise CommandError(f"Could not send due event access emails: {exc}") from exc
                self.stderr.write(f"Event access email check failed: {exc}")

            if not options["loop"]:
                return
            time.sleep(interval)

    def send_due_emails(self):
        now = timezone.now()
        due_events = Event.objects.filter(
            status__in=(
                EventStatus.REGISTRATION_OPEN,
                EventStatus.SCHEDULED,
                EventStatus.LOCKED,
                EventStatus.ACTIVE,
            ),
            start_time__gt=now,
            start_time__lte=now + timedelta(hours=24),
        ).order_by("start_time")

        sent = 0
        failed = 0
        for event in due_events:
            participant_ids = list(
                Participant.objects.filter(
                    event=event,
                    joined_at__isnull=False,
                    event_access_email_sent_at__isnull=True,
                )
                .exclude(email="")
                .values_list("pk", flat=True)
            )
            for participant_id in participant_ids:
                try:
                    sent += send_event_access_email_once(participant_id)
                except Exception as exc:
                    failed += 1
                    self.stderr.write(
                        f"Failed to send event access email for participant "
                        f"{participant_id}: {exc}"
                    )

        if sent or failed or not self.loop_mode:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Sent {sent} event access email(s); failed {failed}."
                )
            )
        if failed:
            raise CommandError(f"Failed to send {failed} event access email(s).")
