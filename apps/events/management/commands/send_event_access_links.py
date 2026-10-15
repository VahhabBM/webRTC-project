from __future__ import annotations

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.events.auth import issue_join_token
from apps.events.models import Event, Participant
from apps.registration.services.email_service import EmailService


class Command(BaseCommand):
    help = "Send personal event-room access links to verified event participants."

    def add_arguments(self, parser):
        parser.add_argument("event_id", help="UUID of the event")
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

        try:
            event = Event.objects.get(pk=options["event_id"])
        except (Event.DoesNotExist, ValidationError, ValueError) as exc:
            raise CommandError("Event not found; check the event UUID.") from exc
        if self.event_end(event) <= timezone.now():
            raise CommandError("The event has already ended; access links would expire.")

        participants = Participant.objects.filter(
            event=event, joined_at__isnull=False
        ).exclude(email="")
        if not participants.exists():
            self.stdout.write("No verified participants with email addresses.")
            return

        sent = 0
        failed = 0
        for participant in participants.iterator():
            try:
                raw_token = issue_join_token(participant)
                join_url = EmailService.build_event_access_url(
                    participant, raw_token
                )
                EmailService.send_event_access_email(
                    recipient_email=participant.email,
                    name=participant.display_name,
                    event_title=event.name,
                    join_url=join_url,
                )
            except Exception as exc:
                failed += 1
                self.stderr.write(
                    f"Failed to send access email for participant {participant.pk}: {exc}"
                )
            else:
                sent += 1

        self.stdout.write(self.style.SUCCESS(f"Sent {sent} event access email(s)."))
        if failed:
            raise CommandError(f"Failed to send {failed} event access email(s).")

    @staticmethod
    def event_end(event):
        return event.start_time + (
            event.num_rounds * event.round_duration
            + max(event.num_rounds - 1, 0) * event.break_duration
        )
