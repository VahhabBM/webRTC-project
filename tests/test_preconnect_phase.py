import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.events.models import Event, Pair, Participant, Round
from apps.events.preconnect import PreconnectService
from apps.events.scheduler import RoundScheduler


@pytest.mark.django_db
class TestPreconnectPhase:
    @pytest.fixture
    def setup_data(self):
        now = timezone.now()
        event = Event.objects.create(
            name="Preconnect Verification Event",
            num_rounds=2,
            round_duration=timedelta(minutes=3),
            break_duration=timedelta(seconds=30),
            start_time=now + timedelta(seconds=30),
        )

        p1 = Participant.objects.create(
            event=event, display_name="User Alpha", join_token_hash="hash_a"
        )
        p2 = Participant.objects.create(
            event=event, display_name="User Beta", join_token_hash="hash_b"
        )

        scheduler = RoundScheduler(event)
        scheduler.persist_schedule()

        r1 = Round.objects.get(event=event, number=1)
        r2 = Round.objects.get(event=event, number=2)

        pair_r1 = Pair.objects.create(
            event=event,
            round=r1,
            participant_a=p1,
            participant_b=p2,
            room_id=str(uuid.uuid4()),
        )

        return {
            "event": event,
            "p1": p1,
            "p2": p2,
            "r1": r1,
            "r2": r2,
            "pair_r1": pair_r1,
            "start_time": event.start_time,
        }

    def test_detect_upcoming_round_in_25s_window(self, setup_data):
        """بررسی شناسایی راند پیش‌رو در پنجره پیش‌اتصال ۲۵ ثانیه‌ای پیش از شروع"""
        service = PreconnectService(setup_data["event"])
        r1 = setup_data["r1"]

        # دقیقا ۲۰ ثانیه مانده به شروع راند ۱ (درون پنجره ۲۵ ثانیه‌ای)
        check_time = r1.starts_at - timedelta(seconds=20)
        upcoming = service.get_upcoming_round(check_time)
        assert upcoming is not None
        assert upcoming.number == 1

        # در فاصله ۶۰ ثانیه قبل (بیرون پنجره ۲۵ ثانیه‌ای) نباید پیدا کند
        far_time = r1.starts_at - timedelta(seconds=60)
        assert service.get_upcoming_round(far_time) is None

    def test_trigger_preconnect_payload_and_status(self, setup_data):
        """بررسی تولید پی‌لود، مشخصات تبادل P2P و تنظیم شمارش معکوس ۲۵ ثانیه‌ای"""
        service = PreconnectService(setup_data["event"])
        r1 = setup_data["r1"]
        check_time = r1.starts_at - timedelta(seconds=25)

        result = service.trigger_preconnect(target_round=r1, now=check_time)
        assert result["status"] == "preconnect_triggered"
        assert result["round_number"] == 1
        assert result["pairs_notified"] == 1
        assert result["seconds_until_start"] == 25

    def test_trigger_preconnect_without_pairs_raises_error(self, setup_data):
        """بررسی بروز خطای اعتبارسنجی در صورت عدم وجود زوج برای راند هدف"""
        service = PreconnectService(setup_data["event"])
        r2 = setup_data["r2"]
        check_time = r2.starts_at - timedelta(seconds=20)

        with pytest.raises(ValidationError):
            service.trigger_preconnect(target_round=r2, now=check_time)

    def test_trigger_round_start_activates_media(self, setup_data):
        """بررسی ارسال سیگنال شروع رسمی راند و فعال‌سازی جریان مدیا در ثانیه صفر"""
        service = PreconnectService(setup_data["event"])
        r1 = setup_data["r1"]

        res = service.trigger_round_start(r1)
        assert res["status"] == "round_started"
        assert res["round_number"] == 1
        assert res["pairs_activated"] == 1
