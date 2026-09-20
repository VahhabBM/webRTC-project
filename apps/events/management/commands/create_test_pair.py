from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.events.auth import issue_join_token
from apps.events.models import Event, Pair, Participant, Round


class Command(BaseCommand):
    help = "Creates a fresh test event, participants and room"

    def add_arguments(self, parser):
        parser.add_argument(
            "--extra-pair",
            action="store_true",
            help=(
                "Create a second isolated pair (test-room-202) in the same event "
                "so T-38 fallback can be verified as pair-scoped."
            ),
        )

    def handle(self, *args, **options):
        # پاک کردن ایونت تستی قبلی تا زمان آن همیشه به‌روز باشد
        Event.objects.filter(name="WebRTC Test Event").delete()

        now = timezone.now()
        event = Event.objects.create(
            name="WebRTC Test Event",
            num_rounds=1,
            round_duration=timedelta(minutes=60),  # زمان کافی برای تست (۱ ساعت)
            break_duration=timedelta(),
            start_time=now,
        )
        p1 = Participant.objects.create(
            event=event, display_name="User_Alpha", join_token_hash="!"
        )
        p2 = Participant.objects.create(
            event=event, display_name="User_Beta", join_token_hash="!"
        )

        round_obj = Round.objects.create(
            event=event,
            number=1,
            starts_at=now,
            ends_at=now + timedelta(minutes=60),
        )
        a1, b1 = sorted((p1, p2), key=lambda participant: str(participant.pk))
        Pair.objects.create(
            event=event,
            round=round_obj,
            participant_a=a1,
            participant_b=b1,
            room_id="test-room-101",
        )

        token1 = issue_join_token(p1)
        token2 = issue_join_token(p2)

        self.stdout.write(
            self.style.SUCCESS(
                f"\nUser 1 Join URL: http://localhost:8000/join/{token1}/"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"User 2 Join URL: http://localhost:8000/join/{token2}/\n"
            )
        )
        self.stdout.write(
            "After opening each join link, navigate to: http://localhost:8000/room/\n"
        )
        self.stdout.write(
            "T-38 selected pair (primary→fallback):\n"
            "  http://localhost:8000/room/"
            "?media_fallback_room=test-room-101&force_media_fallback=1\n"
        )

        if not options.get("extra_pair"):
            return

        p3 = Participant.objects.create(
            event=event, display_name="User_Gamma", join_token_hash="!"
        )
        p4 = Participant.objects.create(
            event=event, display_name="User_Delta", join_token_hash="!"
        )
        a2, b2 = sorted((p3, p4), key=lambda participant: str(participant.pk))
        Pair.objects.create(
            event=event,
            round=round_obj,
            participant_a=a2,
            participant_b=b2,
            room_id="test-room-202",
        )
        token3 = issue_join_token(p3)
        token4 = issue_join_token(p4)
        self.stdout.write(
            self.style.SUCCESS(
                f"\nIsolation pair User 3 Join URL: http://localhost:8000/join/{token3}/"
            )
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Isolation pair User 4 Join URL: http://localhost:8000/join/{token4}/\n"
            )
        )
        self.stdout.write(
            "Isolation pair stays on the primary path:\n  http://localhost:8000/room/\n"
        )
