"""Pure call-room timer and lifecycle logic (T-30).

Mirrors the client-side calculations in call_room.js so they can be
unit-tested without a browser. Uses the same T-15 offset model as
apps.events.clock_sync.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TimerVisualState(StrEnum):
    WAITING = "waiting"
    NORMAL = "normal"
    WARNING = "warning"
    EXPIRED = "expired"


class CallRoomPhase(StrEnum):
    IDLE = "idle"
    PRECONNECTING = "preconnecting"
    IN_ROUND = "in_round"
    ROUND_ENDING = "round_ending"
    EVENT_ENDED = "event_ended"
    DISCONNECTED = "disconnected"


def compute_remaining_ms(
    round_end_ts: int,
    offset_ms: float,
    client_now_ms: int,
) -> int:
    """Milliseconds until round end using clock-sync-adjusted server time."""
    estimated_server_now = int(round(client_now_ms + offset_ms))
    return max(0, round_end_ts - estimated_server_now)


def format_timer_display(remaining_ms: int) -> str:
    """Format remaining milliseconds as MM:SS."""
    total_seconds = max(0, remaining_ms // 1000)
    minutes, seconds = divmod(total_seconds, 60)
    return f"{minutes:02d}:{seconds:02d}"


def timer_visual_state(
    remaining_ms: int,
    warning_threshold_seconds: int,
    *,
    server_warning_active: bool = False,
) -> TimerVisualState:
    if remaining_ms <= 0:
        return TimerVisualState.EXPIRED
    if server_warning_active or remaining_ms <= warning_threshold_seconds * 1000:
        return TimerVisualState.WARNING
    return TimerVisualState.NORMAL


@dataclass(frozen=True)
class PartnerInfo:
    partner_id: str
    room_id: str
    round_number: int
    round_start_ts: int
    round_end_ts: int
    is_offerer: bool
    display_name: str
    tags: tuple[str, ...]


def parse_pairing_payload(payload: dict) -> PartnerInfo:
    """Extract partner metadata from a server.pairing payload."""
    tags_raw = payload.get("partner_tags")
    tags: tuple[str, ...]
    if isinstance(tags_raw, list):
        tags = tuple(str(t) for t in tags_raw)
    else:
        tags = ()

    display_name = payload.get("partner_display_name")
    if not isinstance(display_name, str) or not display_name.strip():
        display_name = "Partner"

    return PartnerInfo(
        partner_id=str(payload["partner_id"]),
        room_id=str(payload["room_id"]),
        round_number=int(payload["round_number"]),
        round_start_ts=int(payload["round_start_ts"]),
        round_end_ts=int(payload["round_end_ts"]),
        is_offerer=bool(payload.get("is_offerer", False)),
        display_name=display_name,
        tags=tags,
    )


@dataclass
class CallRoomState:
    """Reducer state for T-24 lifecycle message handling."""

    phase: CallRoomPhase = CallRoomPhase.IDLE
    round_number: int | None = None
    round_end_ts: int | None = None
    server_warning_active: bool = False
    partner: PartnerInfo | None = None
    event_end_reason: str | None = None

    def on_pairing(self, payload: dict) -> CallRoomState:
        partner = parse_pairing_payload(payload)
        return CallRoomState(
            phase=CallRoomPhase.PRECONNECTING,
            round_number=partner.round_number,
            round_end_ts=partner.round_end_ts,
            server_warning_active=False,
            partner=partner,
            event_end_reason=None,
        )

    def on_round_start(self, payload: dict) -> CallRoomState:
        return CallRoomState(
            phase=CallRoomPhase.IN_ROUND,
            round_number=int(payload.get("round_number", self.round_number or 0)),
            round_end_ts=self.round_end_ts,
            server_warning_active=False,
            partner=self.partner,
            event_end_reason=None,
        )

    def on_round_warning(self, payload: dict) -> CallRoomState:
        round_end_ts = int(payload.get("round_end_ts", self.round_end_ts or 0))
        return CallRoomState(
            phase=self.phase,
            round_number=int(payload.get("round_number", self.round_number or 0)),
            round_end_ts=round_end_ts or self.round_end_ts,
            server_warning_active=True,
            partner=self.partner,
            event_end_reason=None,
        )

    def on_round_end(self, payload: dict) -> CallRoomState:
        return CallRoomState(
            phase=CallRoomPhase.ROUND_ENDING,
            round_number=int(payload.get("round_number", self.round_number or 0)),
            round_end_ts=self.round_end_ts,
            server_warning_active=False,
            partner=None,
            event_end_reason=None,
        )

    def on_event_end(self, payload: dict) -> CallRoomState:
        return CallRoomState(
            phase=CallRoomPhase.EVENT_ENDED,
            round_number=self.round_number,
            round_end_ts=self.round_end_ts,
            server_warning_active=False,
            partner=None,
            event_end_reason=str(payload.get("reason", "completed")),
        )

    def on_disconnect(self) -> CallRoomState:
        return CallRoomState(
            phase=CallRoomPhase.DISCONNECTED,
            round_number=self.round_number,
            round_end_ts=self.round_end_ts,
            server_warning_active=self.server_warning_active,
            partner=self.partner,
            event_end_reason=self.event_end_reason,
        )
