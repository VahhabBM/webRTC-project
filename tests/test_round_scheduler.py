from datetime import timedelta

import pytest
from django.utils import timezone

from apps.events.models import Event, Round
from apps.events.scheduler import (
    EventPhase,
    RoundScheduler,
    calculate_full_schedule,
)


@pytest.mark.django_db
class TestRoundScheduler:
    @pytest.fixture
    def sample_event(self):
        now = timezone.now()
        return Event.objects.create(
            name="Scheduler Test Event",
            num_rounds=3,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=now,
        )

    def test_calculate_full_schedule_structure(self, sample_event):
        """تست صحت محاسبه بازه‌های زمانی راندها و بریک‌ها بر مبنای زمان سرور"""
        preconnect = timedelta(seconds=20)
        schedule = calculate_full_schedule(sample_event, preconnect_duration=preconnect)

        assert len(schedule) == 3
        # راند ۱
        r1 = schedule[0]
        assert r1.number == 1
        assert r1.ends_at - r1.starts_at == timedelta(minutes=5)
        assert r1.starts_at - r1.preconnect_starts_at == preconnect
        assert r1.break_ends_at - r1.ends_at == timedelta(minutes=1)

        # راند ۲ بلافاصله بعد از بریک راند ۱
        r2 = schedule[1]
        assert r2.number == 2
        assert r2.starts_at == r1.break_ends_at

        # آخرین راند نباید بریک بعد از خود داشته باشد
        r3 = schedule[2]
        assert r3.number == 3
        assert r3.break_ends_at is None

    def test_persistence_and_idempotency(self, sample_event):
        """تست ذخیره‌سازی در دیتابیس و عدم ایجاد رکوردهای تکراری در اجرای مجدد"""
        scheduler = RoundScheduler(sample_event)
        rounds_first_run = scheduler.persist_schedule()

        assert Round.objects.filter(event=sample_event).count() == 3
        assert len(rounds_first_run) == 3

        # اجرای مجدد نباید تعداد را زیاد کند (Idempotency)
        rounds_second_run = scheduler.persist_schedule()
        assert Round.objects.filter(event=sample_event).count() == 3
        assert rounds_first_run[0].starts_at == rounds_second_run[0].starts_at

    def test_phase_transitions_timeline(self, sample_event):
        """تست تشخیص دقیق فازهای NOT_STARTED -> PRECONNECT -> IN_ROUND -> BREAK -> COMPLETED"""
        scheduler = RoundScheduler(sample_event)
        scheduler.persist_schedule()

        r1 = Round.objects.get(event=sample_event, number=1)
        r3 = Round.objects.get(event=sample_event, number=3)

        # ۱. قبل از پیش‌اتصال
        phase, _, _ = scheduler.get_current_phase(
            now=r1.starts_at - timedelta(seconds=40)
        )
        assert phase == EventPhase.NOT_STARTED

        # ۲. فاز پیش‌اتصال (۱۰ ثانیه قبل از راند ۱)
        phase, active_r, remaining = scheduler.get_current_phase(
            now=r1.starts_at - timedelta(seconds=10)
        )
        assert phase == EventPhase.PRECONNECT
        assert active_r.number == 1
        assert remaining.total_seconds() == 10

        # ۳. داخل راند ۱
        phase, active_r, _ = scheduler.get_current_phase(
            now=r1.starts_at + timedelta(minutes=2)
        )
        assert phase == EventPhase.IN_ROUND
        assert active_r.number == 1

        # ۴. فاز بریک بین راند ۱ و راند ۲
        phase, active_r, _ = scheduler.get_current_phase(
            now=r1.ends_at + timedelta(seconds=10)
        )
        assert phase == EventPhase.BREAK
        assert active_r.number == 1

        # ۵. فاز پایان رویداد
        phase, active_r, _ = scheduler.get_current_phase(
            now=r3.ends_at + timedelta(seconds=1)
        )
        assert phase == EventPhase.COMPLETED
        assert active_r.number == 3

    def test_restart_safety_recovers_state(self, sample_event):
        """تست بازیابی وضعیت بعد از کرش یا ری‌استارت سرور در میانه راند دوم"""
        scheduler = RoundScheduler(sample_event)
        scheduler.persist_schedule()

        r2 = Round.objects.get(event=sample_event, number=2)
        simulated_crash_time = r2.starts_at + timedelta(minutes=1)

        # ایجاد نمونه جدید از اسکژولر (شبیه‌سازی ری‌استارت پروسس)
        fresh_scheduler = RoundScheduler(sample_event)
        phase, active_r, remaining = fresh_scheduler.get_current_phase(
            now=simulated_crash_time
        )

        assert phase == EventPhase.IN_ROUND
        assert active_r.number == 2
        assert remaining == r2.ends_at - simulated_crash_time

    def test_leader_only_transitions(self, sample_event):
        """تست عدم اجرای گذار توسط نودهای غیرلیدر"""
        scheduler = RoundScheduler(sample_event)
        scheduler.persist_schedule()

        # نود پیرو نباید گذار را اجرا کند
        follower_result = scheduler.execute_transition_if_leader(is_leader=False)
        assert follower_result["executed"] is False
        assert follower_result["reason"] == "not_leader"

        # نود لیدر مجاز به استخراج وضعیت و اجرای گذار است
        leader_result = scheduler.execute_transition_if_leader(is_leader=True)
        assert leader_result["executed"] is True
        assert "phase" in leader_result
        assert "server_time" in leader_result
