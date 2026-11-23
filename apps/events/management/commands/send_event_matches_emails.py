from django.core.management.base import BaseCommand, CommandError

from apps.events.email_notifications import send_event_matches_summary_emails
from apps.events.models import Event, EventStatus


class Command(BaseCommand):
    help = "Send consolidated match summary emails with partner WhatsApp numbers to participants."

    def add_arguments(self, parser):
        parser.add_argument(
            "event_id",
            nargs="?",
            type=str,
            help="UUID of the Event to send match emails for.",
        )
        parser.add_argument(
            "--all-completed",
            action="store_true",
            help="Send match emails for all completed events where emails have not yet been sent.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Resend emails even if already recorded as sent.",
        )

    def handle(self, *args, **options):
        event_id = options.get("event_id")
        all_completed = options.get("all_completed")
        force = options.get("force", False)

        if not event_id and not all_completed:
            raise CommandError("Provide an event_id or specify --all-completed.")

        if event_id:
            try:
                event = Event.objects.get(pk=event_id)
            except Event.DoesNotExist:
                raise CommandError(f"Event {event_id} does not exist.")
            events = [event]
        else:
            events = list(Event.objects.filter(status=EventStatus.COMPLETED))

        total_sent = 0
        for ev in events:
            sent = send_event_matches_summary_emails(ev.pk, force_resend=force)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Sent {sent} match summary email(s) for event '{ev.name}' ({ev.pk})."
                )
            )
            total_sent += sent

        self.stdout.write(
            self.style.SUCCESS(f"Finished. Total match emails sent: {total_sent}.")
        )
