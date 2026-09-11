from django.core.management.base import BaseCommand, CommandError

from apps.events.models import Event, Round
from apps.events.orchestrator import OrchestratorRealtime
from apps.protocol.constants import EventEndReason


class Command(BaseCommand):
    help = (
        "Broadcast T-24 orchestrator protocol messages for an event "
        "(pairing, round_start, warning, round_end, event_end, or dispatch)."
    )

    def add_arguments(self, parser):
        parser.add_argument("--event", required=True, help="Event UUID")
        parser.add_argument(
            "action",
            choices=(
                "pairing",
                "round_start",
                "warning",
                "round_end",
                "event_end",
                "dispatch",
            ),
        )
        parser.add_argument(
            "--round",
            type=int,
            default=None,
            help="Round number (required except for event_end and dispatch)",
        )
        parser.add_argument(
            "--reason",
            default=EventEndReason.COMPLETED,
            choices=[r.value for r in EventEndReason],
        )

    def handle(self, *args, **options):
        try:
            event = Event.objects.get(pk=options["event"])
        except Event.DoesNotExist as exc:
            raise CommandError("Event not found") from exc

        orch = OrchestratorRealtime(event)
        action = options["action"]
        round_number = options["round"]

        if action in {"pairing", "round_start", "warning", "round_end"}:
            if round_number is None:
                raise CommandError("--round is required for this action")
            round_obj = Round.objects.filter(event=event, number=round_number).first()
            if not round_obj:
                raise CommandError(f"Round {round_number} not found for this event")

        if action == "pairing":
            result = orch.broadcast_pairing_sync(round_obj)
        elif action == "round_start":
            result = orch.broadcast_round_start_sync(round_obj)
        elif action == "warning":
            result = orch.broadcast_final_seconds_warning_sync(round_obj)
        elif action == "round_end":
            result = orch.broadcast_round_end_sync(round_obj)
        elif action == "event_end":
            result = orch.broadcast_event_end_sync(reason=options["reason"])
        else:
            result = orch.dispatch_sync()

        self.stdout.write(self.style.SUCCESS(str(result)))
