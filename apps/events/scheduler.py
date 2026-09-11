from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from django.db import transaction
from django.utils import timezone

from apps.events.models import Event, Round


class EventPhase(StrEnum):
    NOT_STARTED = "not_started"
    PRECONNECT = "preconnect"
    IN_ROUND = "in_round"
    BREAK = "break"
    COMPLETED = "completed"


@dataclass(frozen=True)
class ScheduledRound:
    number: int
    preconnect_starts_at: datetime
    starts_at: datetime
    ends_at: datetime
    break_ends_at: datetime | None


def calculate_full_schedule(
    event: Event,
    start_time: datetime | None = None,
    preconnect_duration: timedelta = timedelta(seconds=20),
) -> list[ScheduledRound]:
    """محاسبه جدول زمانی کامل رویداد بر مبنای زمان سرور (بدون وابستگی به DB)"""
    base_start = start_time or event.start_time or timezone.now()
    schedule: list[ScheduledRound] = []

    current_round_start = base_start
    round_dur = event.round_duration
    break_dur = event.break_duration

    for i in range(1, event.num_rounds + 1):
        round_end = current_round_start + round_dur
        preconnect_start = current_round_start - preconnect_duration

        is_last = i == event.num_rounds
        break_end = None if is_last else round_end + break_dur

        schedule.append(
            ScheduledRound(
                number=i,
                preconnect_starts_at=preconnect_start,
                starts_at=current_round_start,
                ends_at=round_end,
                break_ends_at=break_end,
            )
        )

        if not is_last:
            current_round_start = break_end

    return schedule


class RoundScheduler:
    """ارکستریتور و ماشین وضعیت زمان‌بندی راندها"""

    def __init__(
        self,
        event: Event,
        preconnect_duration: timedelta = timedelta(seconds=20),
    ):
        self.event = event
        self.preconnect_duration = preconnect_duration

    @transaction.atomic
    def persist_schedule(self, start_time: datetime | None = None) -> list[Round]:
        """پایدارسازی بازه‌های زمانی در دیتابیس (ایمن در برابر اجرای مجدد)"""
        calculated = calculate_full_schedule(
            self.event,
            start_time=start_time,
            preconnect_duration=self.preconnect_duration,
        )

        existing_rounds = {r.number: r for r in Round.objects.filter(event=self.event)}
        persisted_rounds: list[Round] = []

        for item in calculated:
            if item.number in existing_rounds:
                round_obj = existing_rounds[item.number]
                round_obj.starts_at = item.starts_at
                round_obj.ends_at = item.ends_at
                round_obj.save(update_fields=["starts_at", "ends_at"])
            else:
                round_obj = Round.objects.create(
                    event=self.event,
                    number=item.number,
                    starts_at=item.starts_at,
                    ends_at=item.ends_at,
                )
            persisted_rounds.append(round_obj)

        return persisted_rounds

    def get_current_phase(
        self, now: datetime | None = None
    ) -> tuple[EventPhase, Round | None, timedelta | None]:
        """تشخیص فاز فعلی رویداد بر مبنای زمان مطلق سرور (Restart-Safe)

        خروجی: (فاز رویداد، آبجکت راند مربوطه، زمان باقیمانده تا گذار بعدی)
        """
        current_time = now or timezone.now()
        rounds = list(Round.objects.filter(event=self.event).order_by("number"))

        if not rounds:
            return EventPhase.NOT_STARTED, None, None

        first_round = rounds[0]
        first_preconnect = first_round.starts_at - self.preconnect_duration

        if current_time < first_preconnect:
            return (
                EventPhase.NOT_STARTED,
                None,
                first_preconnect - current_time,
            )

        for round_obj in rounds:
            preconnect_time = round_obj.starts_at - self.preconnect_duration

            # فاز پیش‌اتصال
            if preconnect_time <= current_time < round_obj.starts_at:
                return (
                    EventPhase.PRECONNECT,
                    round_obj,
                    round_obj.starts_at - current_time,
                )

            # فاز داخل راند
            if round_obj.starts_at <= current_time < round_obj.ends_at:
                return (
                    EventPhase.IN_ROUND,
                    round_obj,
                    round_obj.ends_at - current_time,
                )

            # بررسی فاز بریک (فاصله بین پایان این راند تا پیش‌اتصال بعدی)
            next_round = next(
                (r for r in rounds if r.number == round_obj.number + 1), None
            )
            if next_round:
                next_preconnect = next_round.starts_at - self.preconnect_duration
                if round_obj.ends_at <= current_time < next_preconnect:
                    return (
                        EventPhase.BREAK,
                        round_obj,
                        next_preconnect - current_time,
                    )

        # اگر زمان فعلی بعد از پایان آخرین راند باشد
        last_round = rounds[-1]
        if current_time >= last_round.ends_at:
            return EventPhase.COMPLETED, last_round, timedelta(0)

        return EventPhase.NOT_STARTED, None, None

    def execute_transition_if_leader(
        self, is_leader: bool, now: datetime | None = None
    ) -> dict:
        """اجرای گذار وضعیت مشروط به لیدر بودن نود (Leader-Only Transition)"""
        if not is_leader:
            return {"executed": False, "reason": "not_leader"}

        current_time = now or timezone.now()
        phase, active_round, remaining = self.get_current_phase(current_time)

        return {
            "executed": True,
            "phase": phase.value,
            "round_number": active_round.number if active_round else None,
            "remaining_seconds": (remaining.total_seconds() if remaining else 0),
            "server_time": current_time.isoformat(),
        }
