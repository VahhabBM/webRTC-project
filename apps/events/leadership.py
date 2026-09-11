"""Distributed leader election via DB-backed lease (T-22).

Design
------
A single ``LeaderLease`` row per ``lease_name`` is stored in the database.
The unique constraint on ``lease_name`` plus PostgreSQL's row-level locking
guarantees that exactly one process can hold the lease at any given time.

Acquisition protocol (no split-brain)
--------------------------------------
All acquisition logic runs inside a single ``transaction.atomic()`` block:

1. Attempt to UPDATE any existing row for ``lease_name`` whose ``expires_at``
   is in the past (i.e. the previous leader is dead or has released it).
   If exactly one row is updated → this process wins.

2. If no row was updated (no expired row exists), check whether the current
   caller already holds the live lease (renewal path).

3. Otherwise try to INSERT a fresh row.  The unique constraint ensures only
   one concurrent INSERT succeeds; all others raise ``IntegrityError`` and
   return ``False``.

Lease expiry
------------
If the holder dies without calling ``release_lease()``, the lease will
eventually expire (``expires_at < now``) and a new process can acquire it
via step 1 above.  The default TTL is ``LEADER_LEASE_TTL_SECONDS`` (30 s);
callers should renew every TTL/2 seconds.

Thread/process safety
---------------------
The implementation is safe for concurrent calls from multiple OS processes or
threads, provided each call uses a separate database connection (Django's
default per-thread connection pool satisfies this).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from django.db import IntegrityError, transaction
from django.db.models import F
from django.utils import timezone

from apps.events.models import LeaderLease

# Default lease TTL in seconds.  Callers should renew every TTL/2 seconds.
LEADER_LEASE_TTL_SECONDS: int = 30


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def acquire_lease(
    lease_name: str,
    leader_id: str,
    *,
    ttl_seconds: int = LEADER_LEASE_TTL_SECONDS,
) -> bool:
    """Try to acquire or renew the named lease for *leader_id*.

    Returns ``True`` if this caller now holds the lease, ``False`` otherwise.

    The function is idempotent: calling it again as the current (non-expired)
    holder returns ``True`` without changing the lease expiry.  To explicitly
    extend the expiry use ``renew_lease()``.
    """
    now = timezone.now()
    new_expires = now + timedelta(seconds=ttl_seconds)

    with transaction.atomic():
        # Step 1: try to take over an *expired* lease.
        updated = LeaderLease.objects.filter(
            lease_name=lease_name,
            expires_at__lt=now,
        ).update(
            leader_id=leader_id,
            acquired_at=now,
            expires_at=new_expires,
            version=F("version") + 1,
        )
        if updated:
            return True

        # Step 2: check if we already hold a live lease (idempotent renewal).
        try:
            existing = LeaderLease.objects.get(lease_name=lease_name)
            if existing.leader_id == leader_id and existing.expires_at >= now:
                return True
            # Another process holds a live lease → we cannot acquire it.
            return False
        except LeaderLease.DoesNotExist:
            pass

        # Step 3: no row at all → attempt to INSERT.
        try:
            LeaderLease.objects.create(
                lease_name=lease_name,
                leader_id=leader_id,
                acquired_at=now,
                expires_at=new_expires,
                version=1,
            )
            return True
        except IntegrityError:
            # Another concurrent caller won the INSERT race.
            return False


def renew_lease(
    lease_name: str,
    leader_id: str,
    *,
    ttl_seconds: int = LEADER_LEASE_TTL_SECONDS,
) -> bool:
    """Extend the expiry of a lease already held by *leader_id*.

    Returns ``True`` if the renewal succeeded (caller is still the leader),
    ``False`` if the lease has expired or is held by someone else.
    """
    now = timezone.now()
    new_expires = now + timedelta(seconds=ttl_seconds)

    updated = LeaderLease.objects.filter(
        lease_name=lease_name,
        leader_id=leader_id,
        expires_at__gte=now,  # Must still be valid
    ).update(expires_at=new_expires)

    return bool(updated)


def release_lease(lease_name: str, leader_id: str) -> bool:
    """Voluntarily release the lease held by *leader_id*.

    Expires the lease immediately by setting ``expires_at`` to the epoch so
    other instances can acquire it without waiting for the natural TTL.
    Returns ``True`` if the release updated a row (caller was the holder).
    """
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    updated = LeaderLease.objects.filter(
        lease_name=lease_name,
        leader_id=leader_id,
    ).update(expires_at=epoch)
    return bool(updated)


def get_current_leader(lease_name: str) -> LeaderLease | None:
    """Return the current (non-expired) lease for *lease_name*, or ``None``."""
    now = timezone.now()
    try:
        lease = LeaderLease.objects.get(lease_name=lease_name)
        return lease if lease.expires_at > now else None
    except LeaderLease.DoesNotExist:
        return None


def is_leader(lease_name: str, leader_id: str) -> bool:
    """Return ``True`` if *leader_id* currently holds the named lease."""
    lease = get_current_leader(lease_name)
    return lease is not None and lease.leader_id == leader_id
