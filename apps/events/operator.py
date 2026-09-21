from datetime import datetime, timedelta

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils import timezone

from apps.events.models import (
    Event,
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

    @transaction.atomic
    def pause(self) -> dict:
        """متوقف‌سازی موقت راند جاری و فریز کردن زمان باقیمانده"""
        self._ensure_leader()
        now = timezone.now()

        scheduler = RoundScheduler(self.event)
        phase, active_round, remaining = scheduler.get_current_phase(now)

        if phase != EventPhase.IN_ROUND or not active_round or not remaining:
            raise ValidationError("Event cannot be paused outside of an active round.")

        # بررسی اینکه آیا از قبل متوقف است یا خیر
        last_log = self.event.operator_logs.first()
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
            },
            performed_at=now,
        )

        payload = {
            "round_number": active_round.number,
            "remaining_seconds": remaining_seconds,
            "paused_at": now.isoformat(),
        }
        self._broadcast("pause", payload)
        return {"status": "paused", "log_id": str(log.pk), **payload}

    @transaction.atomic
    def resume(self) -> dict:
        """از سرگیری رویداد متوقف‌شده و شیفت دادن راندهای باقیمانده به جلو"""
        self._ensure_leader()
        now = timezone.now()

        last_log = self.event.operator_logs.first()
        if not last_log or last_log.action != OperatorActionType.PAUSE:
            raise ValidationError("Event is not in a paused state.")

        active_round = last_log.round
        if not active_round:
            raise ValidationError("Target round for resume not found.")

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
        subsequent_rounds = Round.objects.filter(
            event=self.event, number__gt=active_round.number
        ).order_by("number")

        for r in subsequent_rounds:
            r.starts_at += time_shift
            r.ends_at += time_shift
            r.save(update_fields=["starts_at", "ends_at"])

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
        self._broadcast("resume", payload)
        return {"status": "resumed", "log_id": str(log.pk), **payload}

    @transaction.atomic
    def extend(self, extra_seconds: int) -> dict:
        """افزایش زمان راند جاری و شیفت دادن راندهای بعدی"""
        self._ensure_leader()
        if extra_seconds <= 0:
            raise ValidationError("Extension duration must be greater than zero.")

        now = timezone.now()
        scheduler = RoundScheduler(self.event)
        phase, active_round, _ = scheduler.get_current_phase(now)

        if phase != EventPhase.IN_ROUND or not active_round:
            raise ValidationError("Cannot extend a round when no round is active.")

        extension = timedelta(seconds=extra_seconds)
        active_round.ends_at += extension
        active_round.save(update_fields=["ends_at"])

        # شیفت راندهای بعدی
        subsequent_rounds = Round.objects.filter(
            event=self.event, number__gt=active_round.number
        ).order_by("number")

        for r in subsequent_rounds:
            r.starts_at += extension
            r.ends_at += extension
            r.save(update_fields=["starts_at", "ends_at"])

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
        self._broadcast("extend", payload)
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
        relay_path_share_percentage = getattr(self.event, "relay_path_share_percentage", 0.0)

        fallback_upgrades_count = getattr(self.event, "fallback_upgrades_count", 0)
        upgrade_percentage = (fallback_upgrades_count / total_sessions) * 100 if total_sessions > 0 else 0.0

        message_latency_ms = getattr(self.event, "message_latency_ms", 0.0)
        drop_rate_one_minute = getattr(self.event, "drop_rate_one_minute", 0.0)

        alerts = []

        # آستانه ۱: سهم مسیر کمکی از ۳۵٪ بیشتر شود
        if relay_path_share_percentage > 35.0:
            alerts.append({
                "type": "HIGH_RELAY_SHARE",
                "message": f"Relay path share is {relay_path_share_percentage:.1f}%, exceeding 35% threshold."
            })

        # آستانه ۲: ارتقا از ۱۰٪ اتاق‌ها بیشتر شود
        if upgrade_percentage > 10.0:
            alerts.append({
                "type": "HIGH_FALLBACK_UPGRADES",
                "message": f"Fallback upgrades are at {upgrade_percentage:.1f}%, exceeding 10% threshold."
            })

        # آستانه ۳: افت متصل‌ها در یک دقیقه بیش از ۵٪ باشد
        if drop_rate_one_minute > 5.0:
            alerts.append({
                "type": "HIGH_CONNECTION_DROP",
                "message": f"Connection drop rate in the last minute is {drop_rate_one_minute:.1f}%, exceeding 5% threshold."
            })

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
