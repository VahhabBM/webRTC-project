from datetime import datetime, timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.events.models import (
    Event,
    EventStatus,
    OperatorActionLog,
    OperatorActionType,
    Round,
)
from apps.events.scheduler import EventPhase, RoundScheduler


class OperatorService:
    """سرویس مدیریت اکشن‌های زنده اپراتور (Pause, Resume, Extend)"""

    def __init__(self, event: Event, is_leader: bool = True):
        self.event = event
        self.is_leader = is_leader

    def _ensure_leader(self) -> None:
        if not self.is_leader:
            raise PermissionDenied(
                "Only the cluster leader node can execute operator actions."
            )

    def _broadcast(self, action: str, payload: dict) -> None:
        """برودکست پیام به تمام شرکت‌کنندگان حاضر در رویداد"""
        channel_layer = get_channel_layer()
        if channel_layer:
            async_to_sync(channel_layer.group_send)(
                f"event_{self.event.pk}",
                {
                    "type": "operator.action",
                    "action": action,
                    "payload": payload,
                },
            )

    def _broadcast_after_commit(self, action: str, payload: dict) -> None:
        transaction.on_commit(lambda: self._broadcast(action, payload))

    @transaction.atomic
    def pause(self) -> dict:
        """متوقف‌سازی موقت راند جاری و فریز کردن زمان باقیمانده"""
        self._ensure_leader()
        locked_event = Event.objects.select_for_update().get(pk=self.event.pk)
        self.event = locked_event
        if locked_event.status == EventStatus.PAUSED:
            raise ValidationError("Event is already paused.")
        now = timezone.now()

        scheduler = RoundScheduler(self.event)
        phase, active_round, remaining = scheduler.get_current_phase(now)

        if phase != EventPhase.IN_ROUND or not active_round or not remaining:
            raise ValidationError("Event cannot be paused outside of an active round.")

        # بررسی اینکه آیا از قبل متوقف است یا خیر
        last_log = locked_event.operator_logs.select_for_update().first()
        if last_log and last_log.action == OperatorActionType.PAUSE:
            raise ValidationError("Event is already paused.")

        remaining_seconds = max(int(remaining.total_seconds()), 0)

        log = OperatorActionLog.objects.create(
            event=self.event,
            round=active_round,
            action=OperatorActionType.PAUSE,
            details={
                "remaining_seconds": remaining_seconds,
                "paused_at": now.isoformat(),
                "status_before_pause": locked_event.status,
            },
            performed_at=now,
        )

        payload = {
            "round_number": active_round.number,
            "remaining_seconds": remaining_seconds,
            "paused_at": now.isoformat(),
        }
        locked_event.status = EventStatus.PAUSED
        locked_event.save(update_fields=["status", "updated_at"])
        self._broadcast_after_commit("pause", payload)
        return {"status": "paused", "log_id": str(log.pk), **payload}

    @transaction.atomic
    def resume(self) -> dict:
        """از سرگیری رویداد متوقف‌شده و شیفت دادن راندهای باقیمانده به جلو"""
        self._ensure_leader()
        locked_event = Event.objects.select_for_update().get(pk=self.event.pk)
        self.event = locked_event
        now = timezone.now()

        last_log = locked_event.operator_logs.select_for_update().first()
        if not last_log or last_log.action != OperatorActionType.PAUSE:
            raise ValidationError("Event is not in a paused state.")

        if not last_log.round_id:
            raise ValidationError("Target round for resume not found.")
        active_round = Round.objects.select_for_update().get(pk=last_log.round_id)

        from apps.events.auth import extend_event_access_deadlines, scheduled_event_end

        previous_event_end = scheduled_event_end(locked_event)

        paused_at_str = last_log.details.get("paused_at")
        if paused_at_str:
            paused_at = datetime.fromisoformat(paused_at_str)
            time_shift = max(now - paused_at, timedelta(seconds=1))
        else:
            time_shift = timedelta(seconds=1)

        # به‌روزرسانی پایان راند متوقف‌شده
        active_round.ends_at += time_shift
        active_round.save(update_fields=["ends_at"])

        # شیفت دادن راندهای بعدی متناسب با وقفه ایجادشده
        subsequent_rounds = Round.objects.select_for_update().filter(
            event=locked_event, number__gt=active_round.number
        ).order_by("number")

        for r in subsequent_rounds:
            r.starts_at += time_shift
            r.ends_at += time_shift
            r.save(update_fields=["starts_at", "ends_at"])

        restored_status = last_log.details.get(
            "status_before_pause", EventStatus.RUNNING
        )
        valid_statuses = {choice for choice, _label in EventStatus.choices}
        if restored_status not in valid_statuses or restored_status == EventStatus.PAUSED:
            restored_status = EventStatus.RUNNING
        locked_event.status = restored_status
        locked_event.save(update_fields=["status", "updated_at"])
        self.event = locked_event
        extend_event_access_deadlines(
            locked_event, previous_event_end, scheduled_event_end(locked_event)
        )

        remaining_seconds = max(int((active_round.ends_at - now).total_seconds()), 0)

        log = OperatorActionLog.objects.create(
            event=self.event,
            round=active_round,
            action=OperatorActionType.RESUME,
            details={
                "resumed_at": now.isoformat(),
                "new_ends_at": active_round.ends_at.isoformat(),
                "shifted_seconds": int(time_shift.total_seconds()),
            },
            performed_at=now,
        )

        payload = {
            "round_number": active_round.number,
            "resumed_at": now.isoformat(),
            "ends_at": active_round.ends_at.isoformat(),
            "remaining_seconds": remaining_seconds,
        }
        self._broadcast_after_commit("resume", payload)
        return {"status": "resumed", "log_id": str(log.pk), **payload}

    @transaction.atomic
    def extend(self, extra_seconds: int) -> dict:
        """افزایش زمان راند جاری و شیفت دادن راندهای بعدی"""
        self._ensure_leader()
        locked_event = Event.objects.select_for_update().get(pk=self.event.pk)
        self.event = locked_event
        if extra_seconds <= 0:
            raise ValidationError("Extension duration must be greater than zero.")

        from apps.events.auth import extend_event_access_deadlines, scheduled_event_end

        now = timezone.now()
        previous_event_end = scheduled_event_end(locked_event)
        scheduler = RoundScheduler(locked_event)
        phase, active_round, _ = scheduler.get_current_phase(now)

        if phase != EventPhase.IN_ROUND or not active_round:
            raise ValidationError("Cannot extend a round when no round is active.")

        extension = timedelta(seconds=extra_seconds)
        active_round.ends_at += extension
        active_round.save(update_fields=["ends_at"])

        # شیفت راندهای بعدی
        subsequent_rounds = Round.objects.select_for_update().filter(
            event=locked_event, number__gt=active_round.number
        ).order_by("number")

        for r in subsequent_rounds:
            r.starts_at += extension
            r.ends_at += extension
            r.save(update_fields=["starts_at", "ends_at"])

        extend_event_access_deadlines(
            locked_event, previous_event_end, scheduled_event_end(locked_event)
        )

        log = OperatorActionLog.objects.create(
            event=self.event,
            round=active_round,
            action=OperatorActionType.EXTEND,
            details={
                "extended_by_seconds": extra_seconds,
                "new_ends_at": active_round.ends_at.isoformat(),
            },
            performed_at=now,
        )

        payload = {
            "round_number": active_round.number,
            "extended_by_seconds": extra_seconds,
            "ends_at": active_round.ends_at.isoformat(),
        }
        self._broadcast_after_commit("extend", payload)
        return {"status": "extended", "log_id": str(log.pk), **payload}

    def recalculate_pairs(
        self,
        *,
        operator_confirmed: bool = False,
        absent_participant_ids: list | set | None = None,
        operator_details: dict | None = None,
    ):
        """بازمحاسبه جفت‌ها پیش از شروع رویداد با تأیید صریح اپراتور (T-42)."""
        self._ensure_leader()
        from apps.events.scheduling import recalculate_pairs_pre_start

        return recalculate_pairs_pre_start(
            event=self.event,
            operator_confirmed=operator_confirmed,
            absent_participant_ids=absent_participant_ids,
            operator_details=operator_details,
        )

    def get_live_metrics(self) -> dict:
        """محاسبه متریک‌های زنده رویداد و بررسی آستانه‌های هشدار (T-43)"""
        active_connections_count = getattr(self.event, "active_connections_count", 0)
        active_rooms_count = getattr(self.event, "active_rooms_count", 0)

        total_sessions = max(active_rooms_count * 2, 1)
        relay_path_share_percentage = getattr(
            self.event, "relay_path_share_percentage", 0.0
        )

        fallback_upgrades_count = getattr(self.event, "fallback_upgrades_count", 0)
        upgrade_percentage = (
            (fallback_upgrades_count / total_sessions) * 100
            if total_sessions > 0
            else 0.0
        )

        message_latency_ms = getattr(self.event, "message_latency_ms", 0.0)
        drop_rate_one_minute = getattr(self.event, "drop_rate_one_minute", 0.0)

        alerts = []

        # آستانه ۱: سهم مسیر کمکی از ۳۵٪ بیشتر شود
        if relay_path_share_percentage > 35.0:
            alerts.append(
                {
                    "type": "HIGH_RELAY_SHARE",
                    "message": f"Relay path share is {relay_path_share_percentage:.1f}%, exceeding 35% threshold.",
                }
            )

        # آستانه ۲: ارتقا از ۱۰٪ اتاق‌ها بیشتر شود
        if upgrade_percentage > 10.0:
            alerts.append(
                {
                    "type": "HIGH_FALLBACK_UPGRADES",
                    "message": f"Fallback upgrades are at {upgrade_percentage:.1f}%, exceeding 10% threshold.",
                }
            )

        # آستانه ۳: افت متصل‌ها در یک دقیقه بیش از ۵٪ باشد
        if drop_rate_one_minute > 5.0:
            alerts.append(
                {
                    "type": "HIGH_CONNECTION_DROP",
                    "message": f"Connection drop rate in the last minute is {drop_rate_one_minute:.1f}%, exceeding 5% threshold.",
                }
            )

        return {
            "active_connections_count": active_connections_count,
            "active_rooms_count": active_rooms_count,
            "relay_path_share_percentage": relay_path_share_percentage,
            "fallback_upgrades_count": fallback_upgrades_count,
            "message_latency_ms": message_latency_ms,
            "drop_rate_one_minute": drop_rate_one_minute,
            "has_critical_alerts": len(alerts) > 0,
            "alerts": alerts,
            "checked_at": timezone.now().isoformat(),
        }
