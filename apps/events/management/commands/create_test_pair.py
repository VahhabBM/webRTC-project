from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from apps.events.auth import issue_join_token
from apps.events.models import Event, Pair, Participant, Round


class Command(BaseCommand):
    help = "Creates a fresh test event, participants and room"

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
        Pair.objects.create(
            event=event,
            round=round_obj,
            participant_a=p1,
            participant_b=p2,
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
