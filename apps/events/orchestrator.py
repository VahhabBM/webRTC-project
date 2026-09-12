"""Real-time orchestrator protocol messages (T-24).

Broadcasts T-13 lifecycle messages to every active WebSocket that belongs
to the event or round, including connections owned by other service
instances, via the shared channel layer (Redis in deployment, in-memory
in tests).

This module does not own Event status (T-22), matching/allocation
persistence (T-20), or round duration math (T-23). Pairing payloads use
stored ``Pair`` rows and ``Round.starts_at`` / ``Round.ends_at``. Send
timestamps use the same UTC Unix-ms clock as T-14/T-15.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from asgiref.sync import async_to_sync
from channels.db import database_sync_to_async
from channels.layers import get_channel_layer
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from apps.events.models import Event, Pair, Round
from apps.events.scheduler import EventPhase, RoundScheduler
from apps.protocol.constants import EventEndReason, MessageType
from apps.protocol.schemas import (
    build_server_event_end,
    build_server_pairing,
    build_server_round_end,
    build_server_round_start,
    build_server_round_warning,
)

logger = logging.getLogger(__name__)

# Channel-layer event type → ParticipantConsumer.orchestrator_message.
ORCHESTRATOR_CHANNEL_TYPE = "orchestrator.message"

DEFAULT_FINAL_SECONDS_WARNING = 30
_SENT_CACHE_TTL_SECONDS = 24 * 60 * 60
_UNSET = object()


def participant_channel_group(participant_id) -> str:
    """Per-connection group used by T-14 after ``server.hello``."""
    return f"participant_{participant_id}"


def event_channel_group(event_id) -> str:
    """Event-wide group used by T-14/T-25 after handshake."""
    return f"event_{event_id}"


def datetime_to_unix_ms(dt: datetime) -> int:
    """Convert an aware (or naive-UTC) datetime to T-13 Unix milliseconds."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


@dataclass(frozen=True)
class PairingRecipient:
    """One ``server.pairing`` destination derived from a stored Pair."""

    participant_id: str
    partner_id: str
    room_id: str
    is_offerer: bool
    partner_display_name: str
    partner_tags: tuple[str, ...]
    group: str


def select_pairing_recipients(round_obj: Round) -> list[PairingRecipient]:
    """Return per-participant pairing destinations for *round_obj*.

    Unmatched participants are omitted. Recipients in other events/rounds
    are never included. ``participant_a`` is the initial offerer so exactly
    one peer per pair has ``is_offerer=True``.
    """
    pairs = (
        Pair.objects.filter(round_id=round_obj.pk, event_id=round_obj.event_id)
        .select_related("participant_a", "participant_b")
        .prefetch_related("participant_a__tags", "participant_b__tags")
    )
    recipients: list[PairingRecipient] = []
    for pair in pairs:
        room_id = str(pair.room_id)
        tags_a = tuple(pair.participant_a.tags.values_list("name", flat=True))
        tags_b = tuple(pair.participant_b.tags.values_list("name", flat=True))
        pid_a = str(pair.participant_a_id)
        pid_b = str(pair.participant_b_id)
        recipients.append(
            PairingRecipient(
                participant_id=pid_a,
                partner_id=pid_b,
                room_id=room_id,
                is_offerer=True,
                partner_display_name=pair.participant_b.display_name,
                partner_tags=tags_b,
                group=participant_channel_group(pid_a),
            )
        )
        recipients.append(
            PairingRecipient(
                participant_id=pid_b,
                partner_id=pid_a,
                room_id=room_id,
                is_offerer=False,
                partner_display_name=pair.participant_a.display_name,
                partner_tags=tags_a,
                group=participant_channel_group(pid_b),
            )
        )
    return recipients


def select_event_broadcast_group(event: Event) -> str:
    """Group that every handshake-complete connection for *event* joins."""
    return event_channel_group(event.pk)


class OrchestratorRealtime:
    """Build T-13 orchestrator messages and fan them out over the channel layer."""

    def __init__(
        self,
        event: Event,
        *,
        is_leader: bool = True,
        channel_layer=_UNSET,
        now_fn: Callable[[], datetime] | None = None,
        final_seconds: int | None = None,
    ):
        self.event = event
        self.is_leader = is_leader
        self.channel_layer = (
            get_channel_layer() if channel_layer is _UNSET else channel_layer
        )
        self.now_fn = now_fn or timezone.now
        if final_seconds is None:
            final_seconds = getattr(
                settings,
                "ORCHESTRATOR_FINAL_SECONDS",
                DEFAULT_FINAL_SECONDS_WARNING,
            )
        self.final_seconds = int(final_seconds)

    def _now(self, now: datetime | None = None) -> datetime:
        current = now if now is not None else self.now_fn()
        if timezone.is_naive(current):
            current = timezone.make_aware(current, UTC)
        return current

    def _server_ts_ms(self, now: datetime | None = None) -> int:
        return datetime_to_unix_ms(self._now(now))

    def _publish(self, group: str, message: dict) -> bool:
        """Publish one protocol envelope. Failures never abort other groups."""
        if not self.channel_layer:
            return False
        try:
            async_to_sync(self.channel_layer.group_send)(
                group,
                {
                    "type": ORCHESTRATOR_CHANNEL_TYPE,
                    "message": message,
                },
            )
            return True
        except Exception:
            logger.exception(
                "Orchestrator failed to publish %s to group %s",
                message.get("type"),
                group,
            )
            return False

    async def _apublish(self, group: str, message: dict) -> bool:
        """Async publish used when the caller already owns the event loop."""
        if not self.channel_layer:
            return False
        try:
            await self.channel_layer.group_send(
                group,
                {
                    "type": ORCHESTRATOR_CHANNEL_TYPE,
                    "message": message,
                },
            )
            return True
        except Exception:
            logger.exception(
                "Orchestrator failed to publish %s to group %s",
                message.get("type"),
                group,
            )
            return False

    def _mark_key(self, key: str) -> str:
        return f"orchestrator:{self.event.pk}:{key}"

    def _already_sent(self, key: str) -> bool:
        return bool(cache.get(self._mark_key(key)))

    def _mark_sent(self, key: str) -> None:
        cache.set(self._mark_key(key), True, timeout=_SENT_CACHE_TTL_SECONDS)

    def _send_once(self, key: str, factory) -> bool:
        if self._already_sent(key):
            return False
        factory()
        self._mark_sent(key)
        return True

    def _pairing_message(self, recipient: PairingRecipient, round_obj: Round) -> dict:
        return build_server_pairing(
            round_number=round_obj.number,
            room_id=recipient.room_id,
            partner_id=recipient.partner_id,
            is_offerer=recipient.is_offerer,
            round_start_ts=datetime_to_unix_ms(round_obj.starts_at),
            round_end_ts=datetime_to_unix_ms(round_obj.ends_at),
            partner_display_name=recipient.partner_display_name,
            partner_tags=list(recipient.partner_tags),
        )

    def broadcast_pairing(
        self, round_obj: Round, *, now: datetime | None = None
    ) -> dict:
        """Send ``server.pairing`` to each allocated participant in the round."""
        del now  # pairing timestamps come from Round, not a parallel clock
        recipients = select_pairing_recipients(round_obj)
        delivered = 0
        for recipient in recipients:
            if self._publish(
                recipient.group, self._pairing_message(recipient, round_obj)
            ):
                delivered += 1
        return {
            "type": MessageType.SERVER_PAIRING,
            "round_number": round_obj.number,
            "recipients": len(recipients),
            "delivered": delivered,
        }

    def broadcast_round_start(
        self, round_obj: Round, *, now: datetime | None = None
    ) -> dict:
        """Send ``server.round_start`` to each paired participant (room-scoped)."""
        server_ts = self._server_ts_ms(now)
        recipients = select_pairing_recipients(round_obj)
        delivered = 0
        for recipient in recipients:
            message = build_server_round_start(
                round_number=round_obj.number,
                room_id=recipient.room_id,
                server_ts=server_ts,
            )
            if self._publish(recipient.group, message):
                delivered += 1
        return {
            "type": MessageType.SERVER_ROUND_START,
            "round_number": round_obj.number,
            "recipients": len(recipients),
            "delivered": delivered,
            "server_ts": server_ts,
        }

    def broadcast_final_seconds_warning(
        self, round_obj: Round, *, now: datetime | None = None
    ) -> dict:
        """Send ``server.round_warning`` to every active connection for the event."""
        current = self._now(now)
        remaining = max(0, int((round_obj.ends_at - current).total_seconds()))
        server_ts = datetime_to_unix_ms(current)
        message = build_server_round_warning(
            round_number=round_obj.number,
            server_ts=server_ts,
            remaining_seconds=remaining,
            round_end_ts=datetime_to_unix_ms(round_obj.ends_at),
        )
        group = select_event_broadcast_group(self.event)
        delivered = 1 if self._publish(group, message) else 0
        return {
            "type": MessageType.SERVER_ROUND_WARNING,
            "round_number": round_obj.number,
            "group": group,
            "delivered": delivered,
            "remaining_seconds": remaining,
            "server_ts": server_ts,
        }

    def broadcast_round_end(
        self, round_obj: Round, *, now: datetime | None = None
    ) -> dict:
        """Send ``server.round_end`` to every active connection for the event."""
        server_ts = self._server_ts_ms(now)
        message = build_server_round_end(
            round_number=round_obj.number, server_ts=server_ts
        )
        group = select_event_broadcast_group(self.event)
        delivered = 1 if self._publish(group, message) else 0
        return {
            "type": MessageType.SERVER_ROUND_END,
            "round_number": round_obj.number,
            "group": group,
            "delivered": delivered,
            "server_ts": server_ts,
        }

    def broadcast_event_end(
        self,
        *,
        reason: str = EventEndReason.COMPLETED,
        message: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        """Send ``server.event_end`` to every active connection for the event."""
        server_ts = self._server_ts_ms(now)
        envelope = build_server_event_end(
            reason=reason, server_ts=server_ts, message=message
        )
        group = select_event_broadcast_group(self.event)
        delivered = 1 if self._publish(group, envelope) else 0
        return {
            "type": MessageType.SERVER_EVENT_END,
            "group": group,
            "delivered": delivered,
            "reason": reason,
            "server_ts": server_ts,
        }

    def dispatch(self, *, now: datetime | None = None) -> dict:
        """Send the next lifecycle message(s) for the scheduler's current phase.

        Leader-only. Idempotent per event/round/message via the shared cache
        so a second instance (or a later tick) does not re-broadcast. Does
        not mutate Event/Round rows (T-20/T-22 stay unchanged).
        """
        if not self.is_leader:
            return {"executed": False, "reason": "not_leader", "sent": []}

        current = self._now(now)
        scheduler = RoundScheduler(self.event)
        phase, active_round, remaining = scheduler.get_current_phase(current)
        sent: list[str] = []

        if phase == EventPhase.PRECONNECT and active_round:
            if self._send_once(
                f"r{active_round.number}:pairing",
                lambda: self.broadcast_pairing(active_round, now=current),
            ):
                sent.append(MessageType.SERVER_PAIRING)
        elif phase == EventPhase.IN_ROUND and active_round:
            if self._send_once(
                f"r{active_round.number}:pairing",
                lambda: self.broadcast_pairing(active_round, now=current),
            ):
                sent.append(MessageType.SERVER_PAIRING)
            if self._send_once(
                f"r{active_round.number}:round_start",
                lambda: self.broadcast_round_start(active_round, now=current),
            ):
                sent.append(MessageType.SERVER_ROUND_START)
            remaining_s = remaining.total_seconds() if remaining else 0
            if remaining_s <= self.final_seconds:
                if self._send_once(
                    f"r{active_round.number}:warning",
                    lambda: self.broadcast_final_seconds_warning(
                        active_round, now=current
                    ),
                ):
                    sent.append(MessageType.SERVER_ROUND_WARNING)
        elif phase == EventPhase.BREAK and active_round:
            if self._send_once(
                f"r{active_round.number}:round_end",
                lambda: self.broadcast_round_end(active_round, now=current),
            ):
                sent.append(MessageType.SERVER_ROUND_END)
        elif phase == EventPhase.COMPLETED:
            if active_round:
                if self._send_once(
                    f"r{active_round.number}:round_end",
                    lambda: self.broadcast_round_end(active_round, now=current),
                ):
                    sent.append(MessageType.SERVER_ROUND_END)
            if self._send_once(
                "event_end",
                lambda: self.broadcast_event_end(now=current),
            ):
                sent.append(MessageType.SERVER_EVENT_END)

        return {
            "executed": True,
            "phase": phase.value,
            "round_number": active_round.number if active_round else None,
            "sent": sent,
            "server_time": current.isoformat(),
        }

    async def abroadcast_prepared(self, jobs: list[tuple[str, dict]]) -> int:
        """Publish already-built (group, envelope) jobs on the running loop."""
        delivered = 0
        for group, message in jobs:
            if await self._apublish(group, message):
                delivered += 1
        return delivered

    def pairing_jobs(self, round_obj: Round) -> list[tuple[str, dict]]:
        return [
            (recipient.group, self._pairing_message(recipient, round_obj))
            for recipient in select_pairing_recipients(round_obj)
        ]

    def round_start_jobs(
        self, round_obj: Round, *, now: datetime | None = None
    ) -> list[tuple[str, dict]]:
        server_ts = self._server_ts_ms(now)
        jobs = []
        for recipient in select_pairing_recipients(round_obj):
            jobs.append(
                (
                    recipient.group,
                    build_server_round_start(
                        round_number=round_obj.number,
                        room_id=recipient.room_id,
                        server_ts=server_ts,
                    ),
                )
            )
        return jobs

    async def abroadcast_pairing(
        self, round_obj: Round, *, now: datetime | None = None
    ) -> dict:
        del now
        jobs = await database_sync_to_async(self.pairing_jobs)(round_obj)
        delivered = await self.abroadcast_prepared(jobs)
        return {
            "type": MessageType.SERVER_PAIRING,
            "round_number": round_obj.number,
            "recipients": len(jobs),
            "delivered": delivered,
        }

    async def abroadcast_round_start(
        self, round_obj: Round, *, now: datetime | None = None
    ) -> dict:
        jobs = await database_sync_to_async(self.round_start_jobs)(round_obj, now=now)
        delivered = await self.abroadcast_prepared(jobs)
        return {
            "type": MessageType.SERVER_ROUND_START,
            "round_number": round_obj.number,
            "recipients": len(jobs),
            "delivered": delivered,
        }

    async def abroadcast_final_seconds_warning(
        self, round_obj: Round, *, now: datetime | None = None
    ) -> dict:
        current = self._now(now)
        remaining = max(0, int((round_obj.ends_at - current).total_seconds()))
        server_ts = datetime_to_unix_ms(current)
        message = build_server_round_warning(
            round_number=round_obj.number,
            server_ts=server_ts,
            remaining_seconds=remaining,
            round_end_ts=datetime_to_unix_ms(round_obj.ends_at),
        )
        group = select_event_broadcast_group(self.event)
        delivered = 1 if await self._apublish(group, message) else 0
        return {
            "type": MessageType.SERVER_ROUND_WARNING,
            "round_number": round_obj.number,
            "group": group,
            "delivered": delivered,
            "remaining_seconds": remaining,
            "server_ts": server_ts,
        }

    async def abroadcast_round_end(
        self, round_obj: Round, *, now: datetime | None = None
    ) -> dict:
        server_ts = self._server_ts_ms(now)
        message = build_server_round_end(
            round_number=round_obj.number, server_ts=server_ts
        )
        group = select_event_broadcast_group(self.event)
        delivered = 1 if await self._apublish(group, message) else 0
        return {
            "type": MessageType.SERVER_ROUND_END,
            "round_number": round_obj.number,
            "group": group,
            "delivered": delivered,
            "server_ts": server_ts,
        }

    async def abroadcast_event_end(self, **kwargs) -> dict:
        server_ts = self._server_ts_ms(kwargs.get("now"))
        reason = kwargs.get("reason", EventEndReason.COMPLETED)
        envelope = build_server_event_end(
            reason=reason,
            server_ts=server_ts,
            message=kwargs.get("message"),
        )
        group = select_event_broadcast_group(self.event)
        delivered = 1 if await self._apublish(group, envelope) else 0
        return {
            "type": MessageType.SERVER_EVENT_END,
            "group": group,
            "delivered": delivered,
            "reason": reason,
            "server_ts": server_ts,
        }

    broadcast_pairing_sync = broadcast_pairing
    broadcast_round_start_sync = broadcast_round_start
    broadcast_final_seconds_warning_sync = broadcast_final_seconds_warning
    broadcast_round_end_sync = broadcast_round_end
    broadcast_event_end_sync = broadcast_event_end
    dispatch_sync = dispatch
