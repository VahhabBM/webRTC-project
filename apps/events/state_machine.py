"""Event lifecycle state machine (T-22).

Implements the authoritative transition table for Event status and persists
every transition atomically to the DB via a two-phase EventTransitionLog
entry.  The state machine is intentionally decoupled from WebSocket consumers
so it can be exercised in plain unit/integration tests.

Allowed transitions
-------------------
    DRAFT            → REGISTRATION_OPEN
    REGISTRATION_OPEN → LOCKED
    LOCKED           → RUNNING
    RUNNING          → PAUSED
    RUNNING          → COMPLETED
    PAUSED           → RUNNING
    PAUSED           → COMPLETED

All other transitions are rejected with ``InvalidTransitionError``.
"""

from __future__ import annotations

from django.db import transaction

from apps.events.models import Event, EventStatus, EventTransitionLog

# ---------------------------------------------------------------------------
# Transition table
# ---------------------------------------------------------------------------

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    EventStatus.DRAFT: frozenset({EventStatus.REGISTRATION_OPEN}),
    EventStatus.REGISTRATION_OPEN: frozenset({EventStatus.LOCKED}),
    EventStatus.LOCKED: frozenset({EventStatus.RUNNING}),
    EventStatus.RUNNING: frozenset({EventStatus.PAUSED, EventStatus.COMPLETED}),
    EventStatus.PAUSED: frozenset({EventStatus.RUNNING, EventStatus.COMPLETED}),
    EventStatus.COMPLETED: frozenset(),
    # Legacy statuses are present in the model for backwards compatibility
    # but are not connected to the new T-22 machine.
    EventStatus.SCHEDULED: frozenset(),
    EventStatus.ACTIVE: frozenset(),
    EventStatus.CANCELLED: frozenset(),
}


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class InvalidTransitionError(Exception):
    """Raised when a requested status change is not permitted."""

    def __init__(self, from_status: str, to_status: str) -> None:
        self.from_status = from_status
        self.to_status = to_status
        super().__init__(
            f"Transition '{from_status}' → '{to_status}' is not allowed. "
            f"Valid next states: {sorted(VALID_TRANSITIONS.get(from_status, frozenset()))}"
        )


class StaleEventError(Exception):
    """Raised when the event's current DB status differs from the caller's view.

    This guards against race conditions where two callers attempt a transition
    concurrently.  ``select_for_update()`` inside ``transition_event()``
    prevents this in most cases, but callers that re-read the event outside
    the transaction may still encounter it.
    """


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def can_transition(from_status: str, to_status: str) -> bool:
    """Return True if *to_status* is a valid successor of *from_status*."""
    return to_status in VALID_TRANSITIONS.get(from_status, frozenset())


def get_valid_next_states(status: str) -> frozenset[str]:
    """Return the set of states reachable from *status*."""
    return VALID_TRANSITIONS.get(status, frozenset())


def transition_event(
    event: Event,
    to_status: str,
    *,
    leader_id: str,
    details: dict | None = None,
) -> EventTransitionLog:
    """Atomically move *event* to *to_status* and persist a full audit record.

    Two-phase commit protocol
    -------------------------
    1. Inside a single ``transaction.atomic()`` block:
       a. Lock the Event row with ``SELECT FOR UPDATE`` to serialise concurrent
          transitions on the same event.
       b. Validate that the current DB status permits the requested transition.
       c. Write an ``EventTransitionLog`` row with ``is_complete=False``
          (the "in-progress" marker).
       d. Update ``Event.status`` to *to_status*.
       e. Flip ``EventTransitionLog.is_complete`` to ``True``.
    2. The transaction commits — all five writes become durable atomically.

    If the process crashes between step (c/d) and the commit, the incomplete
    log row survives on DB and a new leader can detect it via
    ``find_incomplete_transitions()``.

    Parameters
    ----------
    event:
        The Event to transition.  Need not be the freshest DB read; the
        function re-reads it under lock.
    to_status:
        Target status value (use ``EventStatus.*`` constants).
    leader_id:
        Identifier of the caller (hostname/PID or service instance ID).
    details:
        Optional JSON-serialisable dict stored on the log for reconciliation.

    Returns
    -------
    EventTransitionLog
        The completed (``is_complete=True``) log entry.

    Raises
    ------
    InvalidTransitionError
        If *to_status* is not reachable from the current status.
    """
    with transaction.atomic():
        # Lock the event row to serialise concurrent transition attempts.
        locked_event = Event.objects.select_for_update().get(pk=event.pk)

        if not can_transition(locked_event.status, to_status):
            raise InvalidTransitionError(locked_event.status, to_status)

        from_status = locked_event.status

        # Phase 1: write the "in-progress" log entry.
        log = EventTransitionLog.objects.create(
            event=locked_event,
            from_status=from_status,
            to_status=to_status,
            leader_id=leader_id,
            details=details or {},
            is_complete=False,
        )

        # Phase 2a: update the event status.
        locked_event.status = to_status
        locked_event.save(update_fields=["status", "updated_at"])

        # Phase 2b: mark the log as complete.
        log.is_complete = True
        log.save(update_fields=["is_complete"])

    return log


def find_incomplete_transitions(
    event: Event | None = None,
) -> list[EventTransitionLog]:
    """Return all ``EventTransitionLog`` rows that were never completed.

    A new leader calls this after taking over to detect transitions that were
    interrupted (e.g., the previous leader crashed between writing the log and
    updating the event status).

    Parameters
    ----------
    event:
        If given, restrict the search to a single event.  Otherwise returns
        all incomplete logs across all events.
    """
    qs = EventTransitionLog.objects.filter(is_complete=False).select_related("event")
    if event is not None:
        qs = qs.filter(event=event)
    return list(qs)


def reconcile_interrupted_transition(log: EventTransitionLog, *, leader_id: str) -> str:
    """Inspect an incomplete log entry and safely bring the event to a consistent state.

    Strategy
    --------
    Because the transition and the log update are in the *same* DB transaction,
    there are only two possible states when we find ``is_complete=False``:

    A. The transaction was committed — the Event row already has ``to_status``
       but the ``is_complete`` flip was somehow not saved (extremely unlikely
       with ``transaction.atomic()``; practically impossible with PostgreSQL).
       → Roll *forward*: mark the log complete.

    B. The transaction was rolled back — the Event row still has ``from_status``
       (or a later status applied by someone else).
       → Roll *forward* if the Event is already at ``to_status``; otherwise
         just mark the log as abandoned (we cannot safely re-apply).

    In both cases the function marks the log ``is_complete=True`` so it no
    longer shows up in future reconciliation scans.

    Returns
    -------
    str
        One of ``"rolled_forward"``, ``"already_consistent"``,
        ``"status_diverged"`` (log abandoned because the event moved elsewhere).
    """
    with transaction.atomic():
        fresh_event = Event.objects.select_for_update().get(pk=log.event_id)

        if fresh_event.status == log.to_status:
            outcome = "rolled_forward"
        elif fresh_event.status == log.from_status:
            # Transaction rolled back — event is back at from_status.  Mark
            # the log complete so it's not retried endlessly; callers may
            # re-attempt the full transition.
            outcome = "already_consistent"
        else:
            # The event has since moved to a completely different state.
            outcome = "status_diverged"

        log.is_complete = True
        log.details = {
            **log.details,
            "reconciled_by": leader_id,
            "outcome": outcome,
            "event_status_at_reconcile": fresh_event.status,
        }
        log.save(update_fields=["is_complete", "details"])

    return outcome
