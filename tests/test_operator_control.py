from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.events.models import (
    Event,
    EventStatus,
    OperatorActionLog,
    OperatorActionType,
    Round,
)
from apps.events.operator import OperatorService
from apps.events.scheduler import RoundScheduler


@pytest.mark.django_db
class TestOperatorControl:
    @pytest.fixture
    def setup_event(self):
        now = timezone.now()
        event = Event.objects.create(
            name="Operator Test Event",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=now,
        )
        scheduler = RoundScheduler(event)
        scheduler.persist_schedule()
        return event

    def test_non_leader_cannot_execute_operator_actions(self, setup_event):
        """تست عدم دسترسی نودهای پیرو (غیر لیدر) به متدهای کنترلی"""
        service = OperatorService(setup_event, is_leader=False)
        with pytest.raises(PermissionDenied):
            service.pause()

        with pytest.raises(PermissionDenied):
            service.resume()

        with pytest.raises(PermissionDenied):
            service.extend(60)

    def test_pause_and_resume_preserves_remaining_time(self, setup_event):
        """تست فریز شدن زمان در حالت Pause و اعمال دقیق آن پس از Resume"""
        service = OperatorService(setup_event, is_leader=True)
        r1 = Round.objects.get(event=setup_event, number=1)
        r2 = Round.objects.get(event=setup_event, number=2)
        initial_r2_start = r2.starts_at

        # اجرای Pause
        pause_res = service.pause()
        assert pause_res["status"] == "paused"
        assert pause_res["round_number"] == 1
        assert pause_res["remaining_seconds"] > 0
        setup_event.refresh_from_db()
        assert setup_event.status == EventStatus.PAUSED
        paused_phase, paused_round, frozen_remaining = RoundScheduler(
            setup_event
        ).get_current_phase(now=r1.ends_at + timedelta(minutes=1))
        assert paused_phase.value == "paused"
        assert paused_round.pk == r1.pk
        assert int(frozen_remaining.total_seconds()) == pause_res["remaining_seconds"]

        # بررسی ثبت لاگ و اتصال به راند جاری
        log = OperatorActionLog.objects.filter(event=setup_event).first()
        assert log.action == OperatorActionType.PAUSE
        assert log.round == r1

        # اجرای مجدد نباید مجاز باشد
        with pytest.raises(ValidationError):
            service.pause()

        # اجرای Resume
        resume_res = service.resume()
        assert resume_res["status"] == "resumed"
        setup_event.refresh_from_db()
        assert setup_event.status == EventStatus.DRAFT

        # بررسی شیفت پیدا کردن راند دوم
        r2.refresh_from_db()
        assert r2.starts_at > initial_r2_start

        r1.refresh_from_db()
        frozen_end = r1.ends_at
        with pytest.raises(ValidationError, match="operator pause"):
            RoundScheduler(setup_event).persist_schedule(
                start_time=setup_event.start_time - timedelta(hours=1)
            )
        r1.refresh_from_db()
        assert r1.ends_at == frozen_end

    def test_extend_round_shifts_future_rounds(self, setup_event):
        """تست افزایش زمان راند جاری و جابجایی زمان راندهای آینده"""
        service = OperatorService(setup_event, is_leader=True)
        r1 = Round.objects.get(event=setup_event, number=1)
        r2 = Round.objects.get(event=setup_event, number=2)

        old_r1_end = r1.ends_at
        old_r2_start = r2.starts_at

        # تمدید راند به مدت ۱۲۰ ثانیه
        result = service.extend(extra_seconds=120)
        assert result["status"] == "extended"

        r1.refresh_from_db()
        r2.refresh_from_db()

        assert r1.ends_at == old_r1_end + timedelta(seconds=120)
        assert r2.starts_at == old_r2_start + timedelta(seconds=120)

        frozen_end = r1.ends_at
        with pytest.raises(ValidationError, match="operator pause"):
            RoundScheduler(setup_event).persist_schedule(
                start_time=setup_event.start_time - timedelta(hours=1)
            )
        r1.refresh_from_db()
        assert r1.ends_at == frozen_end

    @pytest.mark.parametrize(
        "prior_status",
        [EventStatus.SCHEDULED, EventStatus.ACTIVE, EventStatus.RUNNING],
    )
    def test_pause_resume_restores_pre_pause_event_status(self, setup_event, prior_status):
        setup_event.status = prior_status
        setup_event.save(update_fields=["status"])
        service = OperatorService(setup_event, is_leader=True)

        service.pause()
        setup_event.refresh_from_db()
        assert setup_event.status == EventStatus.PAUSED
        service.resume()
        setup_event.refresh_from_db()
        assert setup_event.status == prior_status
