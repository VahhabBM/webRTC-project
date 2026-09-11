from datetime import timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.events.models import Event, Pair, Round
from apps.events.scheduler import EventPhase, RoundScheduler


class PreconnectService:
    PRECONNECT_WINDOW_SECONDS = 20

    def __init__(self, event: Event):
        self.event = event
        self.channel_layer = get_channel_layer()

    def get_upcoming_round(self, now: timezone.datetime | None = None) -> Round | None:

        now = now or timezone.now()
        scheduler = RoundScheduler(self.event)
        phase, target_round, remaining = scheduler.get_current_phase(now)

        if phase == EventPhase.PRECONNECT and target_round:
            return target_round

        upcoming_window_end = now + timedelta(seconds=self.PRECONNECT_WINDOW_SECONDS)
        return (
            Round.objects.filter(
                event=self.event,
                starts_at__gt=now,
                starts_at__lte=upcoming_window_end,
            )
            .order_by("number")
            .first()
        )

    def trigger_preconnect(
        self,
        target_round: Round | None = None,
        now: timezone.datetime | None = None,
    ) -> dict:
        now = now or timezone.now()
        round_obj = target_round or self.get_upcoming_round(now)

        if not round_obj:
            raise ValidationError(
                "No upcoming round found within the preconnect window."
            )

        pairs = Pair.objects.filter(event=self.event, round=round_obj).select_related(
            "participant_a", "participant_b"
        )

        if not pairs.exists():
            raise ValidationError(f"No pairs configured for round {round_obj.number}.")

        seconds_until_start = max(int((round_obj.starts_at - now).total_seconds()), 0)
        notified_pairs = 0

        for pair in pairs:
            payload_for_a = {
                "round_number": round_obj.number,
                "room_id": str(pair.room_id),
                "peer_id": str(pair.participant_b.id),
                "peer_name": pair.participant_b.display_name,
                "is_initiator": True,
                "starts_at": round_obj.starts_at.isoformat(),
                "seconds_until_start": seconds_until_start,
                "media_muted": True,
            }

            payload_for_b = {
                "round_number": round_obj.number,
                "room_id": str(pair.room_id),
                "peer_id": str(pair.participant_a.id),
                "peer_name": pair.participant_a.display_name,
                "is_initiator": False,
                "starts_at": round_obj.starts_at.isoformat(),
                "seconds_until_start": seconds_until_start,
                "media_muted": True,
            }

            if self.channel_layer:
                async_to_sync(self.channel_layer.group_send)(
                    f"participant_{pair.participant_a.id}",
                    {"type": "round.preconnect", "payload": payload_for_a},
                )
                async_to_sync(self.channel_layer.group_send)(
                    f"participant_{pair.participant_b.id}",
                    {"type": "round.preconnect", "payload": payload_for_b},
                )

                async_to_sync(self.channel_layer.group_send)(
                    f"room_{pair.room_id}",
                    {
                        "type": "round.preconnect",
                        "payload": {
                            "round_number": round_obj.number,
                            "room_id": str(pair.room_id),
                            "starts_at": round_obj.starts_at.isoformat(),
                            "seconds_until_start": seconds_until_start,
                        },
                    },
                )
            notified_pairs += 1

        return {
            "status": "preconnect_triggered",
            "round_number": round_obj.number,
            "pairs_notified": notified_pairs,
            "seconds_until_start": seconds_until_start,
        }

    def trigger_round_start(self, round_obj: Round) -> dict:
        pairs = Pair.objects.filter(event=self.event, round=round_obj)
        if self.channel_layer:
            for pair in pairs:
                async_to_sync(self.channel_layer.group_send)(
                    f"room_{pair.room_id}",
                    {
                        "type": "round.start",
                        "payload": {
                            "round_number": round_obj.number,
                            "room_id": str(pair.room_id),
                            "ends_at": (
                                round_obj.ends_at.isoformat()
                                if round_obj.ends_at
                                else None
                            ),
                            "media_muted": False,
                        },
                    },
                )
        return {
            "status": "round_started",
            "round_number": round_obj.number,
            "pairs_activated": pairs.count(),
        }
