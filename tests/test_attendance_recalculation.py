import uuid
from datetime import timedelta

import pytest
from django.core.exceptions import PermissionDenied, ValidationError
from django.utils import timezone

from apps.events.models import (
    Event,
    EventStatus,
    OperatorActionLog,
    OperatorActionType,
    Pair,
    PairStatus,
    Participant,
    Round,
)
from apps.events.operator import OperatorService
from apps.events.scheduling import persist_schedule, recalculate_pairs_pre_start


@pytest.mark.django_db
class TestAttendanceRecalculationT42:
    @pytest.fixture
    def setup_event(self):
        now = timezone.now()
        return Event.objects.create(
            name="Pre-Start Matching Event",
            num_rounds=2,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=now,
            status=EventStatus.LOCKED,
        )

    def test_recalculate_requires_explicit_operator_confirmation(self, setup_event):
        """تست اجباری بودن تأیید صریح اپراتور"""
        with pytest.raises(PermissionDenied, match="confirmation is required"):
            recalculate_pairs_pre_start(setup_event, operator_confirmed=False)

    def test_recalculate_forbidden_when_running_or_completed(self, setup_event):
        """تست عدم اجازه اجرا در وضعیت در حال اجرا یا پایان یافته"""
        setup_event.status = EventStatus.RUNNING
        setup_event.save(update_fields=["status"])

        with pytest.raises(
            ValidationError, match="only allowed before the event starts"
        ):
            recalculate_pairs_pre_start(setup_event, operator_confirmed=True)

    def test_absent_participants_removed_and_operator_log_created(self, setup_event):
        """تست سناریوی حذف غایبان، جایگزینی اتمیک جدول و ثبت لاگ اپراتور"""
        participants = [
            Participant.objects.create(
                event=setup_event,
                display_name=f"User_{i}",
                join_token_hash="!",
            )
            for i in range(10)
        ]

        absent_pids = [str(participants[0].id), str(participants[1].id)]

        schedule = recalculate_pairs_pre_start(
            setup_event,
            operator_confirmed=True,
            absent_participant_ids=absent_pids,
            operator_details={"reason": "pre-start no show"},
        )

        assert schedule is not None
        assert setup_event.participants.count() == 8
        assert not setup_event.participants.filter(id__in=absent_pids).exists()

        for rnd in schedule.rounds:
            for pair in rnd.pairs:
                assert pair.pid_a not in absent_pids
                assert pair.pid_b not in absent_pids
            if rnd.unmatched_pid:
                assert rnd.unmatched_pid not in absent_pids

        log = OperatorActionLog.objects.filter(
            event=setup_event, action=OperatorActionType.RECALCULATE
        ).first()
        assert log is not None
        assert log.details["confirmed"] is True
        assert log.details["absent_removed_count"] == 2
        assert log.details["violation_count"] == 0

    def test_definition_of_done_955_participants_with_25_absent(self):
        """معیار پذیرش اصلی (DoD): ۹۵۵ شرکت‌کننده، ۲۵ غایب، نقض قید صفر"""
        now = timezone.now()
        event = Event.objects.create(
            name="Scale DoD Event",
            num_rounds=1,
            round_duration=timedelta(minutes=5),
            break_duration=timedelta(minutes=1),
            start_time=now,
            status=EventStatus.LOCKED,
        )

        participants = [
            Participant(
                id=uuid.uuid4(),
                event=event,
                display_name=f"Person_{i:04d}",
                join_token_hash="!",
            )
            for i in range(955)
        ]
        Participant.objects.bulk_create(participants)

        absent_sample = participants[:25]
        absent_ids = [str(p.id) for p in absent_sample]

        operator = OperatorService(event=event, is_leader=True)
        schedule = operator.recalculate_pairs(
            operator_confirmed=True,
            absent_participant_ids=absent_ids,
        )

        assert event.participants.count() == 930
        assert not event.participants.filter(id__in=absent_ids).exists()

        all_paired_pids = set()
        for rnd in schedule.rounds:
            for pair in rnd.pairs:
                all_paired_pids.add(pair.pid_a)
                all_paired_pids.add(pair.pid_b)
            if rnd.unmatched_pid:
                all_paired_pids.add(rnd.unmatched_pid)

        for absent_id in absent_ids:
            assert absent_id not in all_paired_pids

    @pytest.mark.parametrize(
        "evidence",
        [
            {"live_allocated": True},
            {"media_received_a": True},
        ],
    )
    def test_schedule_regeneration_preserves_live_room_and_history(
        self, setup_event, evidence
    ):
        setup_event.status = EventStatus.SCHEDULED
        setup_event.save(update_fields=["status"])
        round_obj = Round.objects.create(
            event=setup_event,
            number=1,
            starts_at=setup_event.start_time,
            ends_at=setup_event.start_time + timedelta(minutes=5),
        )
        participant_a = Participant.objects.create(
            event=setup_event, display_name="Live A", join_token_hash="!"
        )
        participant_b = Participant.objects.create(
            event=setup_event, display_name="Live B", join_token_hash="!"
        )
        pair = Pair.objects.create(
            event=setup_event,
            round=round_obj,
            participant_a=participant_a,
            participant_b=participant_b,
            room_id="preserve-this-live-room",
            status=PairStatus.CANCELLED,
            **evidence,
        )

        with pytest.raises(ValidationError, match="live pair allocation"):
            persist_schedule(setup_event, schedule=None)
        with pytest.raises(ValidationError, match="live pair allocation"):
            recalculate_pairs_pre_start(
                setup_event,
                operator_confirmed=True,
                absent_participant_ids={str(participant_a.pk)},
            )

        assert Round.objects.filter(pk=round_obj.pk).exists()
        assert setup_event.participants.filter(pk=participant_a.pk).exists()
        pair.refresh_from_db()
        assert pair.room_id == "preserve-this-live-room"
        assert pair.status == PairStatus.CANCELLED
