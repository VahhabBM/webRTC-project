"""Focused tests for T-22: Event state machine, DB persistence, and leader election.

Test categories
---------------
1. State machine — valid/invalid transitions, rejection messages.
2. DB persistence — EventTransitionLog created with correct fields.
3. Two-phase commit — is_complete progression and interrupted-transition detection.
4. Leader election — acquisition, idempotency, concurrent race, expiry/takeover.
5. Lease lifecycle — renewal, release, is_leader helper.
6. Interrupted-transition reconciliation — new leader finds and fixes incomplete logs.
7. Independence from WebSocket — state machine works with no WS infrastructure.
"""

from __future__ import annotations

import threading
import time
from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone

from apps.events.leadership import (
    acquire_lease,
    get_current_leader,
    is_leader,
    release_lease,
    renew_lease,
)
from apps.events.models import (
    Event,
    EventStatus,
    EventTransitionLog,
    LeaderLease,
)
from apps.events.state_machine import (
    VALID_TRANSITIONS,
    InvalidTransitionError,
    can_transition,
    find_incomplete_transitions,
    get_valid_next_states,
    reconcile_interrupted_transition,
    transition_event,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def event(db):
    return Event.objects.create(
        name="T-22 Test Event",
        num_rounds=3,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(seconds=30),
        start_time=timezone.now(),
        status=EventStatus.DRAFT,
    )


@pytest.fixture
def leader_id():
    return "test-host:1234"


# ---------------------------------------------------------------------------
# 1. State machine — valid/invalid transitions
# ---------------------------------------------------------------------------


class TestTransitionTable:
    """Unit tests for the VALID_TRANSITIONS table and helper functions."""

    def test_all_t22_states_present(self):
        expected = {
            EventStatus.DRAFT,
            EventStatus.REGISTRATION_OPEN,
            EventStatus.LOCKED,
            EventStatus.RUNNING,
            EventStatus.PAUSED,
            EventStatus.COMPLETED,
        }
        assert expected.issubset(set(VALID_TRANSITIONS.keys()))

    def test_valid_forward_transitions(self):
        assert can_transition(EventStatus.DRAFT, EventStatus.REGISTRATION_OPEN)
        assert can_transition(EventStatus.REGISTRATION_OPEN, EventStatus.LOCKED)
        assert can_transition(EventStatus.LOCKED, EventStatus.RUNNING)
        assert can_transition(EventStatus.RUNNING, EventStatus.PAUSED)
        assert can_transition(EventStatus.RUNNING, EventStatus.COMPLETED)
        assert can_transition(EventStatus.PAUSED, EventStatus.RUNNING)
        assert can_transition(EventStatus.PAUSED, EventStatus.COMPLETED)

    def test_invalid_transitions_rejected(self):
        # Cannot skip states
        assert not can_transition(EventStatus.DRAFT, EventStatus.LOCKED)
        assert not can_transition(EventStatus.DRAFT, EventStatus.RUNNING)
        assert not can_transition(EventStatus.DRAFT, EventStatus.COMPLETED)
        assert not can_transition(EventStatus.REGISTRATION_OPEN, EventStatus.RUNNING)
        assert not can_transition(EventStatus.LOCKED, EventStatus.PAUSED)
        # Cannot go backwards
        assert not can_transition(EventStatus.RUNNING, EventStatus.DRAFT)
        assert not can_transition(EventStatus.RUNNING, EventStatus.REGISTRATION_OPEN)
        assert not can_transition(EventStatus.RUNNING, EventStatus.LOCKED)
        assert not can_transition(EventStatus.PAUSED, EventStatus.DRAFT)
        assert not can_transition(EventStatus.COMPLETED, EventStatus.RUNNING)
        # COMPLETED is terminal
        assert not can_transition(EventStatus.COMPLETED, EventStatus.PAUSED)

    def test_get_valid_next_states(self):
        assert get_valid_next_states(EventStatus.RUNNING) == frozenset(
            {EventStatus.PAUSED, EventStatus.COMPLETED}
        )
        assert get_valid_next_states(EventStatus.COMPLETED) == frozenset()

    def test_unknown_status_returns_empty(self):
        assert not can_transition("nonexistent_status", EventStatus.DRAFT)


# ---------------------------------------------------------------------------
# 2. DB persistence — transitions saved correctly
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTransitionPersistence:
    def test_transition_updates_event_status(self, event, leader_id):
        log = transition_event(
            event, EventStatus.REGISTRATION_OPEN, leader_id=leader_id
        )
        event.refresh_from_db()
        assert event.status == EventStatus.REGISTRATION_OPEN
        assert log.to_status == EventStatus.REGISTRATION_OPEN

    def test_transition_creates_log_entry(self, event, leader_id):
        transition_event(event, EventStatus.REGISTRATION_OPEN, leader_id=leader_id)
        assert EventTransitionLog.objects.filter(event=event).count() == 1

    def test_log_fields_are_correct(self, event, leader_id):
        log = transition_event(
            event,
            EventStatus.REGISTRATION_OPEN,
            leader_id=leader_id,
            details={"reason": "test"},
        )
        assert log.from_status == EventStatus.DRAFT
        assert log.to_status == EventStatus.REGISTRATION_OPEN
        assert log.leader_id == leader_id
        assert log.details == {"reason": "test"}
        assert log.is_complete is True
        assert log.event_id == event.pk

    def test_multiple_transitions_accumulate_logs(self, event, leader_id):
        transition_event(event, EventStatus.REGISTRATION_OPEN, leader_id=leader_id)
        event.refresh_from_db()
        transition_event(event, EventStatus.LOCKED, leader_id=leader_id)
        event.refresh_from_db()
        transition_event(event, EventStatus.RUNNING, leader_id=leader_id)

        logs = list(
            EventTransitionLog.objects.filter(event=event).order_by("transitioned_at")
        )
        assert len(logs) == 3
        assert logs[0].from_status == EventStatus.DRAFT
        assert logs[1].from_status == EventStatus.REGISTRATION_OPEN
        assert logs[2].from_status == EventStatus.LOCKED
        assert all(lg.is_complete for lg in logs)

    def test_full_happy_path_to_completed(self, event, leader_id):
        """Walk the entire lifecycle: DRAFT → ... → COMPLETED."""
        path = [
            EventStatus.REGISTRATION_OPEN,
            EventStatus.LOCKED,
            EventStatus.RUNNING,
            EventStatus.PAUSED,
            EventStatus.RUNNING,
            EventStatus.COMPLETED,
        ]
        for target in path:
            event.refresh_from_db()
            transition_event(event, target, leader_id=leader_id)

        event.refresh_from_db()
        assert event.status == EventStatus.COMPLETED
        assert EventTransitionLog.objects.filter(event=event).count() == len(path)

    def test_invalid_transition_raises_error(self, event, leader_id):
        with pytest.raises(InvalidTransitionError) as exc_info:
            transition_event(event, EventStatus.RUNNING, leader_id=leader_id)
        assert "draft" in str(exc_info.value).lower()
        assert "running" in str(exc_info.value).lower()

    def test_invalid_transition_does_not_persist(self, event, leader_id):
        """A rejected transition must not leave any DB artefacts."""
        with pytest.raises(InvalidTransitionError):
            transition_event(event, EventStatus.RUNNING, leader_id=leader_id)
        event.refresh_from_db()
        assert event.status == EventStatus.DRAFT
        assert EventTransitionLog.objects.filter(event=event).count() == 0

    def test_transition_to_completed_is_terminal(self, event, leader_id):
        transition_event(event, EventStatus.REGISTRATION_OPEN, leader_id=leader_id)
        event.refresh_from_db()
        transition_event(event, EventStatus.LOCKED, leader_id=leader_id)
        event.refresh_from_db()
        transition_event(event, EventStatus.RUNNING, leader_id=leader_id)
        event.refresh_from_db()
        transition_event(event, EventStatus.COMPLETED, leader_id=leader_id)
        event.refresh_from_db()

        with pytest.raises(InvalidTransitionError):
            transition_event(event, EventStatus.RUNNING, leader_id=leader_id)


# ---------------------------------------------------------------------------
# 3. Two-phase commit — is_complete flag
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestTwoPhaseCommit:
    def test_log_is_complete_true_after_success(self, event, leader_id):
        log = transition_event(
            event, EventStatus.REGISTRATION_OPEN, leader_id=leader_id
        )
        log.refresh_from_db()
        assert log.is_complete is True

    def test_find_incomplete_returns_empty_when_all_complete(self, event, leader_id):
        transition_event(event, EventStatus.REGISTRATION_OPEN, leader_id=leader_id)
        assert find_incomplete_transitions(event=event) == []

    def test_find_incomplete_detects_orphaned_log(self, event, leader_id):
        """Simulate a crash by manually creating an is_complete=False row."""
        orphan = EventTransitionLog.objects.create(
            event=event,
            from_status=EventStatus.DRAFT,
            to_status=EventStatus.REGISTRATION_OPEN,
            leader_id="crashed-leader:999",
            is_complete=False,
        )
        incomplete = find_incomplete_transitions(event=event)
        assert len(incomplete) == 1
        assert incomplete[0].pk == orphan.pk

    def test_find_incomplete_without_event_filter(self, db, leader_id):
        """find_incomplete_transitions() with no argument returns all orphaned logs."""
        e1 = Event.objects.create(
            name="E1",
            num_rounds=1,
            round_duration=timedelta(minutes=1),
            break_duration=timedelta(seconds=0),
            start_time=timezone.now(),
        )
        e2 = Event.objects.create(
            name="E2",
            num_rounds=1,
            round_duration=timedelta(minutes=1),
            break_duration=timedelta(seconds=0),
            start_time=timezone.now(),
        )
        EventTransitionLog.objects.create(
            event=e1,
            from_status="draft",
            to_status="registration_open",
            leader_id="l1",
            is_complete=False,
        )
        EventTransitionLog.objects.create(
            event=e2,
            from_status="draft",
            to_status="registration_open",
            leader_id="l2",
            is_complete=False,
        )
        result = find_incomplete_transitions()
        pks = {r.pk for r in result}
        assert len(pks) >= 2  # at least the two we just created


# ---------------------------------------------------------------------------
# 4. Interrupted-transition reconciliation
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestReconciliation:
    def test_reconcile_when_event_already_at_to_status(self, event, leader_id):
        """Event is already at to_status → rolled_forward."""
        event.status = EventStatus.REGISTRATION_OPEN
        event.save(update_fields=["status"])

        orphan = EventTransitionLog.objects.create(
            event=event,
            from_status=EventStatus.DRAFT,
            to_status=EventStatus.REGISTRATION_OPEN,
            leader_id="crashed-leader:999",
            is_complete=False,
        )
        outcome = reconcile_interrupted_transition(orphan, leader_id=leader_id)
        assert outcome == "rolled_forward"

        orphan.refresh_from_db()
        assert orphan.is_complete is True
        assert orphan.details["outcome"] == "rolled_forward"
        assert orphan.details["reconciled_by"] == leader_id

    def test_reconcile_when_event_still_at_from_status(self, event, leader_id):
        """Event rolled back to from_status → already_consistent."""
        # event is at DRAFT (from_status); to_status was REGISTRATION_OPEN
        orphan = EventTransitionLog.objects.create(
            event=event,
            from_status=EventStatus.DRAFT,
            to_status=EventStatus.REGISTRATION_OPEN,
            leader_id="crashed-leader:999",
            is_complete=False,
        )
        outcome = reconcile_interrupted_transition(orphan, leader_id=leader_id)
        assert outcome == "already_consistent"
        orphan.refresh_from_db()
        assert orphan.is_complete is True

    def test_reconcile_when_event_status_diverged(self, event, leader_id):
        """Event has moved to a third state → status_diverged."""
        event.status = EventStatus.LOCKED
        event.save(update_fields=["status"])

        orphan = EventTransitionLog.objects.create(
            event=event,
            from_status=EventStatus.DRAFT,
            to_status=EventStatus.REGISTRATION_OPEN,
            leader_id="crashed-leader:999",
            is_complete=False,
        )
        outcome = reconcile_interrupted_transition(orphan, leader_id=leader_id)
        assert outcome == "status_diverged"
        orphan.refresh_from_db()
        assert orphan.is_complete is True

    def test_reconcile_marks_log_complete_in_all_cases(self, event, leader_id):
        for status in (
            EventStatus.DRAFT,
            EventStatus.REGISTRATION_OPEN,
            EventStatus.LOCKED,
        ):
            event.status = status
            event.save(update_fields=["status"])
            orphan = EventTransitionLog.objects.create(
                event=event,
                from_status=EventStatus.DRAFT,
                to_status=EventStatus.REGISTRATION_OPEN,
                leader_id="old-leader",
                is_complete=False,
            )
            reconcile_interrupted_transition(orphan, leader_id=leader_id)
            orphan.refresh_from_db()
            assert orphan.is_complete is True


# ---------------------------------------------------------------------------
# 5. Leader election — basic operations
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLeaderElectionBasic:
    def test_acquire_creates_lease(self, db):
        result = acquire_lease("test-lease", "leader-A")
        assert result is True
        lease = LeaderLease.objects.get(lease_name="test-lease")
        assert lease.leader_id == "leader-A"
        assert lease.expires_at > timezone.now()

    def test_second_acquire_fails_while_live(self, db):
        acquire_lease("test-lease", "leader-A")
        result = acquire_lease("test-lease", "leader-B")
        assert result is False

    def test_same_leader_acquire_is_idempotent(self, db):
        acquire_lease("test-lease", "leader-A")
        result = acquire_lease("test-lease", "leader-A")
        assert result is True

    def test_expired_lease_can_be_acquired_by_another(self, db):
        # Create an already-expired lease for leader-A.
        past = timezone.now() - timedelta(seconds=60)
        LeaderLease.objects.create(
            lease_name="test-lease",
            leader_id="leader-A",
            acquired_at=past - timedelta(seconds=30),
            expires_at=past,
        )
        result = acquire_lease("test-lease", "leader-B")
        assert result is True
        lease = LeaderLease.objects.get(lease_name="test-lease")
        assert lease.leader_id == "leader-B"

    def test_expired_lease_increments_version(self, db):
        past = timezone.now() - timedelta(seconds=60)
        LeaderLease.objects.create(
            lease_name="test-lease",
            leader_id="leader-A",
            acquired_at=past - timedelta(seconds=30),
            expires_at=past,
            version=3,
        )
        acquire_lease("test-lease", "leader-B")
        lease = LeaderLease.objects.get(lease_name="test-lease")
        assert lease.version == 4

    def test_renew_lease_extends_expiry(self, db):
        acquire_lease("test-lease", "leader-A", ttl_seconds=10)
        original_expiry = LeaderLease.objects.get(lease_name="test-lease").expires_at

        time.sleep(0.05)  # tiny pause so clock advances

        renewed = renew_lease("test-lease", "leader-A", ttl_seconds=30)
        assert renewed is True
        new_expiry = LeaderLease.objects.get(lease_name="test-lease").expires_at
        assert new_expiry > original_expiry

    def test_renew_fails_for_wrong_leader(self, db):
        acquire_lease("test-lease", "leader-A")
        result = renew_lease("test-lease", "leader-B")
        assert result is False

    def test_release_lease_expires_it(self, db):
        acquire_lease("test-lease", "leader-A")
        released = release_lease("test-lease", "leader-A")
        assert released is True
        current = get_current_leader("test-lease")
        assert current is None

    def test_release_fails_for_wrong_leader(self, db):
        acquire_lease("test-lease", "leader-A")
        result = release_lease("test-lease", "leader-B")
        assert result is False
        # leader-A still holds it
        assert is_leader("test-lease", "leader-A")

    def test_get_current_leader_none_when_no_lease(self, db):
        assert get_current_leader("no-such-lease") is None

    def test_get_current_leader_none_when_expired(self, db):
        past = timezone.now() - timedelta(seconds=1)
        LeaderLease.objects.create(
            lease_name="test-lease",
            leader_id="leader-A",
            acquired_at=past,
            expires_at=past,
        )
        assert get_current_leader("test-lease") is None

    def test_is_leader_true_for_current_holder(self, db):
        acquire_lease("test-lease", "leader-A")
        assert is_leader("test-lease", "leader-A") is True
        assert is_leader("test-lease", "leader-B") is False

    def test_different_lease_names_are_independent(self, db):
        acquire_lease("lease-X", "leader-A")
        acquire_lease("lease-Y", "leader-B")
        assert is_leader("lease-X", "leader-A")
        assert is_leader("lease-Y", "leader-B")
        assert not is_leader("lease-X", "leader-B")


# ---------------------------------------------------------------------------
# 6. Concurrent leader acquisition (deterministic, single-process)
# ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
class TestConcurrentLeaderAcquisition:
    """Verify that concurrent acquire_lease() calls produce exactly one winner.

    We use real threads (each with its own DB connection) to exercise the
    unique-constraint / UPDATE race path without mocking.
    """

    def test_only_one_winner_from_concurrent_acquires(self):
        results: list[bool] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def try_acquire(lid: str) -> None:
            try:
                barrier.wait(timeout=5)  # synchronise both threads at start
                ok = acquire_lease("concurrent-lease", lid, ttl_seconds=30)
                results.append(ok)
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        t1 = threading.Thread(target=try_acquire, args=("leader-1",))
        t2 = threading.Thread(target=try_acquire, args=("leader-2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Threads raised: {errors}"
        assert len(results) == 2
        assert results.count(True) == 1, f"Expected exactly one True, got {results}"
        assert results.count(False) == 1

    def test_concurrent_transition_on_same_event(self, db):
        """Two threads racing to transition the same event: exactly one succeeds."""
        event = Event.objects.create(
            name="Race Event",
            num_rounds=1,
            round_duration=timedelta(minutes=1),
            break_duration=timedelta(seconds=0),
            start_time=timezone.now(),
            status=EventStatus.DRAFT,
        )

        successes: list[bool] = []
        errors: list[Exception] = []
        barrier = threading.Barrier(2)

        def try_transition(lid: str) -> None:
            try:
                barrier.wait(timeout=5)
                transition_event(event, EventStatus.REGISTRATION_OPEN, leader_id=lid)
                successes.append(True)
            except InvalidTransitionError:
                successes.append(False)
            except Exception as exc:
                errors.append(exc)
            finally:
                connection.close()

        t1 = threading.Thread(target=try_transition, args=("leader-1",))
        t2 = threading.Thread(target=try_transition, args=("leader-2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Threads raised: {errors}"
        assert len(successes) == 2
        assert successes.count(True) == 1, (
            f"Expected exactly one success, got {successes}"
        )

        # DB must be in the new status with exactly one log entry.
        event.refresh_from_db()
        assert event.status == EventStatus.REGISTRATION_OPEN
        assert EventTransitionLog.objects.filter(event=event).count() == 1


# ---------------------------------------------------------------------------
# 7. State machine independence from WebSocket
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestWebSocketIndependence:
    """Verify state machine works with zero WebSocket infrastructure."""

    def test_transition_without_channel_layer(self, event, leader_id):
        """No channel layer or consumer needed to drive state transitions."""
        log = transition_event(
            event, EventStatus.REGISTRATION_OPEN, leader_id=leader_id
        )
        assert log.is_complete is True
        event.refresh_from_db()
        assert event.status == EventStatus.REGISTRATION_OPEN

    def test_leadership_without_channel_layer(self, db):
        """Leader election requires only the DB, not Redis or channels."""
        ok = acquire_lease("standalone-lease", "instance-no-redis")
        assert ok is True


# ---------------------------------------------------------------------------
# 8. Lease is_valid property
# ---------------------------------------------------------------------------


@pytest.mark.django_db
class TestLeaseModel:
    def test_is_valid_true_for_future_expiry(self, db):
        lease = LeaderLease.objects.create(
            lease_name="v-lease",
            leader_id="l",
            acquired_at=timezone.now(),
            expires_at=timezone.now() + timedelta(seconds=60),
        )
        assert lease.is_valid is True

    def test_is_valid_false_for_past_expiry(self, db):
        lease = LeaderLease.objects.create(
            lease_name="v-lease",
            leader_id="l",
            acquired_at=timezone.now() - timedelta(seconds=120),
            expires_at=timezone.now() - timedelta(seconds=60),
        )
        assert lease.is_valid is False
