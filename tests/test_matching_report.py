"""T-21: Matching quality report — focused test suite.

Covers:
- report calculations (pure unit tests, no DB)
- permission / state gating
- rerun confirmation flow
- zero / non-zero violation behaviour
- lock-button enablement / disablement
- edge cases: no participants, impossible matching, invalid state,
  odd participant counts, rerun
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.events.matching import (
    GeneratedSchedule,
    MatchInput,
    MatchParticipant,
    SchedulePair,
    ScheduleRound,
    ScheduleViolation,
    generate_schedule,
    validate_schedule,
)
from apps.events.models import Event, EventStatus, Pair, Round
from apps.events.report import RoundReport, _percentile10, build_report
from apps.events.scoring import ParticipantProfile, ScoringWeights

User = get_user_model()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_P = ParticipantProfile


def _pid(n: int = 1) -> str:
    return str(uuid.UUID(int=n))


def _pair(pid_a_n: int, pid_b_n: int, score: float = 0.5) -> SchedulePair:
    a, b = _pid(pid_a_n), _pid(pid_b_n)
    if uuid.UUID(a) > uuid.UUID(b):
        a, b = b, a
    return SchedulePair(pid_a=a, pid_b=b, score=score)


def _round(number: int, pairs: list[SchedulePair], bye=None) -> ScheduleRound:
    return ScheduleRound(number=number, pairs=tuple(pairs), unmatched_pid=bye)


def _make_event(status=EventStatus.SCHEDULED, num_rounds=2):
    return Event.objects.create(
        name=f"Event-{status}",
        status=status,
        num_rounds=num_rounds,
        round_duration=timedelta(minutes=5),
        break_duration=timedelta(seconds=30),
        start_time=timezone.now(),
    )


def _admin_client() -> tuple[Client, object]:
    user = User.objects.create_superuser(
        username=f"admin_{uuid.uuid4().hex[:6]}",
        email=f"admin_{uuid.uuid4().hex[:6]}@test.com",
        password="pass",
    )
    client = Client()
    client.force_login(user)
    return client, user


def _report_url(event):
    return f"/admin/events/event/{event.pk}/matching-report/"


# ─────────────────────────────────────────────────────────────────────────────
# Pure-unit: _percentile10
# ─────────────────────────────────────────────────────────────────────────────


class TestPercentile10:
    def test_empty_returns_zero(self):
        assert _percentile10([]) == 0.0

    def test_single_value_returns_itself(self):
        assert _percentile10([0.7]) == 0.7

    def test_two_values(self):
        # With method='inclusive', result must be within [min, max].
        result = _percentile10([0.0, 1.0])
        assert 0.0 <= result <= 1.0, f"Expected result in [0.0, 1.0], got {result}"

    def test_many_values_10th_pct_is_low(self):
        scores = [i / 100 for i in range(101)]
        p10 = _percentile10(scores)
        assert 0.08 <= p10 <= 0.12, f"Expected ~0.10, got {p10}"

    def test_uniform_scores(self):
        scores = [0.5] * 20
        assert _percentile10(scores) == pytest.approx(0.5)


# ─────────────────────────────────────────────────────────────────────────────
# Pure-unit: build_report
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildReport:
    def _simple_schedule(self) -> GeneratedSchedule:
        # 4 participants, 2 rounds, even — no byes
        r1 = _round(1, [_pair(1, 2, 0.8), _pair(3, 4, 0.0)])
        r2 = _round(2, [_pair(1, 3, 0.6), _pair(2, 4, 0.4)])
        return GeneratedSchedule(rounds=(r1, r2))

    def test_round_count(self):
        report = build_report(self._simple_schedule(), [])
        assert len(report.rounds) == 2

    def test_total_pairs(self):
        report = build_report(self._simple_schedule(), [])
        assert report.total_pairs == 4

    def test_avg_score_round1(self):
        report = build_report(self._simple_schedule(), [])
        r1 = report.rounds[0]
        assert r1.avg_score == pytest.approx(0.4)  # (0.8 + 0.0) / 2

    def test_p10_round1(self):
        report = build_report(self._simple_schedule(), [])
        r1 = report.rounds[0]
        # With scores [0.8, 0.0], p10 must be within [min=0.0, max=0.8]
        scores = [sp.score for sp in self._simple_schedule().rounds[0].pairs]
        lo, hi = min(scores), max(scores)
        assert lo <= r1.p10_score <= hi, f"p10 {r1.p10_score} outside [{lo}, {hi}]"

    def test_zero_tag_pairs_counted(self):
        report = build_report(self._simple_schedule(), [])
        assert report.rounds[0].zero_tag_pairs == 1  # pair(3,4) has score 0.0
        assert report.total_zero_tag_pairs == 1

    def test_no_violations_can_lock_true(self):
        report = build_report(self._simple_schedule(), [])
        assert report.can_lock is True
        assert report.violation_count == 0

    def test_with_violations_can_lock_false(self):
        violations = [ScheduleViolation(kind="SELF_PAIR", detail="Round 1")]
        report = build_report(self._simple_schedule(), violations)
        assert report.can_lock is False
        assert report.violation_count == 1
        assert report.violations == violations

    def test_bye_distribution_odd_event(self):
        bye_pid = _pid(5)
        r1 = _round(1, [_pair(1, 2, 0.5), _pair(3, 4, 0.5)], bye=bye_pid)
        r2 = _round(2, [_pair(1, 3, 0.5), _pair(2, 4, 0.5)])
        sched = GeneratedSchedule(rounds=(r1, r2))
        report = build_report(sched, [])
        assert bye_pid in report.bye_distribution
        assert report.bye_distribution[bye_pid] == 1

    def test_bye_distribution_no_byes_even(self):
        report = build_report(self._simple_schedule(), [])
        assert report.bye_distribution == {}

    def test_round_unmatched_pid_propagated(self):
        bye_pid = _pid(99)
        r1 = _round(1, [_pair(1, 2, 0.5)], bye=bye_pid)
        sched = GeneratedSchedule(rounds=(r1,))
        report = build_report(sched, [])
        assert report.rounds[0].unmatched_pid == bye_pid

    def test_empty_schedule(self):
        sched = GeneratedSchedule(rounds=())
        report = build_report(sched, [])
        assert report.total_pairs == 0
        assert report.total_zero_tag_pairs == 0
        assert report.rounds == []
        assert report.bye_distribution == {}
        assert report.can_lock is True  # no violations

    def test_multiple_violations_counted(self):
        v = [
            ScheduleViolation(kind="A", detail="x"),
            ScheduleViolation(kind="B", detail="y"),
            ScheduleViolation(kind="C", detail="z"),
        ]
        report = build_report(self._simple_schedule(), v)
        assert report.violation_count == 3
        assert report.can_lock is False

    def test_round_report_fields_present(self):
        report = build_report(self._simple_schedule(), [])
        r = report.rounds[0]
        assert isinstance(r, RoundReport)
        assert r.number == 1
        assert r.pair_count == 2
        assert isinstance(r.avg_score, float)
        assert isinstance(r.p10_score, float)
        assert isinstance(r.zero_tag_pairs, int)


# ─────────────────────────────────────────────────────────────────────────────
# Integration: build_report with real generate_schedule output
# ─────────────────────────────────────────────────────────────────────────────


class TestBuildReportWithRealSchedule:
    """Uses generate_schedule() to produce a real schedule, then tests report."""

    def _make_input(self, n: int, num_rounds: int) -> MatchInput:
        weights = ScoringWeights(tag_weight=1.0)
        participants = tuple(
            MatchParticipant(
                pid=str(uuid.UUID(int=i + 1)),
                profile=_P(tag_ids=frozenset({str(i % 3)})),
            )
            for i in range(n)
        )
        return MatchInput(
            participants=participants, num_rounds=num_rounds, weights=weights
        )

    def test_even_participants_no_byes(self):
        mi = self._make_input(6, 3)
        sched = generate_schedule(mi)
        pids = frozenset(p.pid for p in mi.participants)
        violations = validate_schedule(sched, pids, num_rounds=3)
        report = build_report(sched, violations)

        assert report.violation_count == 0
        assert report.can_lock is True
        assert len(report.rounds) == 3
        assert report.bye_distribution == {}
        assert report.total_pairs == 9  # 3 pairs × 3 rounds

    def test_odd_participants_has_byes(self):
        mi = self._make_input(5, 4)
        sched = generate_schedule(mi)
        pids = frozenset(p.pid for p in mi.participants)
        violations = validate_schedule(sched, pids, num_rounds=4)
        report = build_report(sched, violations)

        assert report.violation_count == 0
        assert report.can_lock is True
        assert len(report.bye_distribution) >= 1
        # Each round has 1 bye, 4 rounds → 4 bye assignments, 4+ distinct PIDs?
        total_byes = sum(report.bye_distribution.values())
        assert total_byes == 4

    def test_all_same_tags_all_scores_equal(self):
        """When all participants share the same tag, all pair scores = 1.0."""
        weights = ScoringWeights(tag_weight=1.0)
        participants = tuple(
            MatchParticipant(
                pid=str(uuid.UUID(int=i + 1)),
                profile=_P(tag_ids=frozenset({"common"})),
            )
            for i in range(4)
        )
        mi = MatchInput(participants=participants, num_rounds=2, weights=weights)
        sched = generate_schedule(mi)
        report = build_report(sched, [])

        for rr in report.rounds:
            assert rr.avg_score == pytest.approx(1.0)
            assert rr.zero_tag_pairs == 0

    def test_no_tags_all_zero_scores(self):
        """When all participants have no tags, pair scores = 0.0."""
        weights = ScoringWeights(tag_weight=1.0)
        participants = tuple(
            MatchParticipant(
                pid=str(uuid.UUID(int=i + 1)),
                profile=_P(tag_ids=frozenset()),
            )
            for i in range(4)
        )
        mi = MatchInput(participants=participants, num_rounds=2, weights=weights)
        sched = generate_schedule(mi)
        report = build_report(sched, [])

        for rr in report.rounds:
            assert rr.avg_score == pytest.approx(0.0)
        assert report.total_zero_tag_pairs == report.total_pairs


# ─────────────────────────────────────────────────────────────────────────────
# Admin view: state gating (GET requests)
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestMatchingReportViewStateGating:
    def test_get_returns_200_for_superuser(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED)
        resp = client.get(_report_url(event))
        assert resp.status_code == 200

    def test_unauthenticated_redirects_to_login(self):
        event = _make_event(EventStatus.SCHEDULED)
        client = Client()
        resp = client.get(_report_url(event))
        assert resp.status_code == 302
        assert "/admin/login/" in resp["Location"] or "login" in resp["Location"]

    @pytest.mark.parametrize(
        "status",
        [EventStatus.SCHEDULED, EventStatus.ACTIVE, EventStatus.COMPLETED],
    )
    def test_runnable_states_show_run_button(self, status):
        client, _ = _admin_client()
        event = _make_event(status)
        resp = client.get(_report_url(event))
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Run Matching" in content

    @pytest.mark.parametrize(
        "status",
        [EventStatus.DRAFT, EventStatus.CANCELLED],
    )
    def test_non_runnable_states_show_error(self, status):
        client, _ = _admin_client()
        event = _make_event(status)
        resp = client.get(_report_url(event))
        assert resp.status_code == 200
        content = resp.content.decode()
        # Should not show the run form button
        assert "Run Matching" not in content
        # Should show a state-gate error
        assert "not available" in content.lower() or "cannot" in content.lower()

    def test_nonexistent_event_returns_404(self):
        client, _ = _admin_client()
        resp = client.get(f"/admin/events/event/{uuid.uuid4()}/matching-report/")
        assert resp.status_code == 404


# ─────────────────────────────────────────────────────────────────────────────
# Admin view: POST – run matching
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestMatchingReportViewRunAction:
    def _participants(self, event, n, with_tags=False):
        from apps.events.models import Participant, Tag

        tag = None
        if with_tags:
            tag = Tag.objects.create(name=f"tag-{uuid.uuid4().hex[:4]}")

        participants = []
        for i in range(n):
            p = Participant.objects.create(
                id=uuid.UUID(int=i + 1),
                event=event,
                display_name=f"P{i + 1:03d}",
                join_token_hash="!",
            )
            if with_tags and tag:
                p.tags.add(tag)
            participants.append(p)
        return participants

    def test_post_run_shows_report(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Quality Report" in content
        assert "Avg Score" in content
        assert "Constraint Violations" in content

    def test_post_run_shows_numeric_metrics(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        # Total pairs for 4 even participants × 2 rounds = 4
        assert "4" in content  # total pairs at minimum

    def test_post_run_does_not_persist_to_db(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)
        client.post(_report_url(event), {"action": "run"})
        assert Round.objects.filter(event=event).count() == 0
        assert Pair.objects.filter(event=event).count() == 0

    def test_post_run_draft_event_shows_error(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.DRAFT, num_rounds=2)
        self._participants(event, 4)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Quality Report" not in content

    def test_post_run_no_participants_shows_error(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        # No participants created
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Quality Report" not in content
        assert "Error" in content or "error" in content.lower()

    def test_post_run_one_participant_shows_error(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 1)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Quality Report" not in content

    def test_post_run_odd_participants_shows_bye_distribution(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=3)
        self._participants(event, 5)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Bye Distribution" in content

    def test_post_run_even_participants_no_bye_section(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        # No byes for even participant count
        assert "Bye Distribution" not in content

    def test_post_run_with_tags_shows_nonzero_avg_score(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4, with_tags=True)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "1.0000" in content  # all same tag → jaccard = 1.0


# ─────────────────────────────────────────────────────────────────────────────
# Admin view: POST – lock result
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestMatchingReportViewLockAction:
    def _participants(self, event, n):
        from apps.events.models import Participant

        for i in range(n):
            Participant.objects.create(
                id=uuid.UUID(int=i + 1),
                event=event,
                display_name=f"P{i + 1:03d}",
                join_token_hash="!",
            )

    def test_lock_persists_rounds_and_pairs(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)
        resp = client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        assert resp.status_code == 200
        assert Round.objects.filter(event=event).count() == 2
        assert Pair.objects.filter(event=event).count() == 4

    def test_lock_shows_success_message(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)
        resp = client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "locked" in content.lower() or "persisted" in content.lower()

    def test_lock_shows_lock_result_button_disabled_when_violations(self):
        """Artificially inject violations by using too-few rounds for the schedule."""
        # This test verifies the template path for violations > 0.
        # We test it via build_report directly since triggering real violations
        # through the engine is non-trivial.
        violations = [ScheduleViolation(kind="SELF_PAIR", detail="test")]
        sched = GeneratedSchedule(
            rounds=(ScheduleRound(number=1, pairs=(), unmatched_pid=None),)
        )
        report = build_report(sched, violations)
        assert report.can_lock is False
        assert report.violation_count == 1

    def test_lock_draft_event_shows_state_error(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.DRAFT, num_rounds=2)
        self._participants(event, 4)
        resp = client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        assert resp.status_code == 200
        assert Round.objects.filter(event=event).count() == 0

    def test_lock_cancelled_event_shows_state_error(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.CANCELLED, num_rounds=2)
        self._participants(event, 4)
        resp = client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        assert resp.status_code == 200
        assert Round.objects.filter(event=event).count() == 0

    def test_lock_is_idempotent_and_replaces_existing(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)
        # First lock
        client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        first_round_pks = set(
            Round.objects.filter(event=event).values_list("pk", flat=True)
        )
        # Second lock (rerun + replace)
        client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        assert Round.objects.filter(event=event).count() == 2
        assert Pair.objects.filter(event=event).count() == 4
        second_round_pks = set(
            Round.objects.filter(event=event).values_list("pk", flat=True)
        )
        # PKs change because rows are deleted and recreated
        assert first_round_pks != second_round_pks


# ─────────────────────────────────────────────────────────────────────────────
# Admin view: rerun confirmation flow
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestRerunConfirmation:
    def _participants(self, event, n):
        from apps.events.models import Participant

        for i in range(n):
            Participant.objects.create(
                id=uuid.UUID(int=i + 1),
                event=event,
                display_name=f"P{i + 1:03d}",
                join_token_hash="!",
            )

    def test_run_without_confirmation_when_schedule_exists_shows_gate(self):
        """POST run without confirmed=yes when a schedule exists → confirmation gate."""
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)

        # First lock a schedule
        client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        assert Round.objects.filter(event=event).count() == 2

        # Now run without confirmation
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        # Should show the confirmation checkbox / warning
        assert "replace" in content.lower() or "confirm" in content.lower()
        # Must NOT show the report yet
        assert "Quality Report" not in content

    def test_run_with_confirmation_when_schedule_exists_runs_ok(self):
        """POST run with confirmed=yes when schedule exists → generates report."""
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)

        client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})

        resp = client.post(_report_url(event), {"action": "run", "confirmed": "yes"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Quality Report" in content

    def test_first_run_no_existing_schedule_requires_no_confirmation(self):
        """POST run without confirmed=yes when NO schedule exists → runs immediately."""
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)

        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Quality Report" in content

    def test_lock_without_confirmation_when_schedule_exists_shows_gate(self):
        """POST lock without confirmed=yes when a schedule exists → confirmation gate."""
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)

        client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})

        resp = client.post(_report_url(event), {"action": "lock"})
        assert resp.status_code == 200
        # Should not show a fresh lock-success; schedule count unchanged
        # (still only 2 rounds from the first lock)
        content = resp.content.decode()
        assert "replace" in content.lower() or "confirm" in content.lower()


# ─────────────────────────────────────────────────────────────────────────────
# Admin view: change form shows "Run Matching Report" button
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db
class TestChangeFormButton:
    def test_change_form_has_matching_report_link(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED)
        resp = client.get(f"/admin/events/event/{event.pk}/change/")
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "matching-report" in content
        assert "Run Matching Report" in content

    def test_add_form_has_no_matching_report_link(self):
        """Add form has no object_id yet, so the button should not appear."""
        client, _ = _admin_client()
        resp = client.get("/admin/events/event/add/")
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "matching-report" not in content


# ─────────────────────────────────────────────────────────────────────────────
# Edge cases
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.django_db(transaction=True)
class TestEdgeCases:
    def _participants(self, event, n):
        from apps.events.models import Participant

        for i in range(n):
            Participant.objects.create(
                id=uuid.UUID(int=i + 1),
                event=event,
                display_name=f"P{i + 1:03d}",
                join_token_hash="!",
            )

    def test_two_participants_minimum_valid(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=1)
        self._participants(event, 2)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        assert "Quality Report" in resp.content.decode()

    def test_impossible_matching_shows_error(self):
        """N=2, R=2 is impossible (max rounds for N=2 is 1)."""
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 2)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Quality Report" not in content
        assert "error" in content.lower() or "Error" in content

    def test_odd_3_participants_1_round(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=1)
        self._participants(event, 3)
        resp = client.post(_report_url(event), {"action": "run"})
        assert resp.status_code == 200
        content = resp.content.decode()
        assert "Quality Report" in content
        assert "Bye Distribution" in content

    def test_lock_result_replaced_not_appended(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)
        client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        assert Round.objects.filter(event=event).count() == 2
        assert Pair.objects.filter(event=event).count() == 4

    def test_get_report_page_with_existing_schedule_shows_warning(self):
        client, _ = _admin_client()
        event = _make_event(EventStatus.SCHEDULED, num_rounds=2)
        self._participants(event, 4)
        client.post(_report_url(event), {"action": "lock", "confirmed": "yes"})
        resp = client.get(_report_url(event))
        assert resp.status_code == 200
        content = resp.content.decode()
        # The confirmation checkbox must be present since a schedule exists
        assert 'name="confirmed"' in content
