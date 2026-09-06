from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class MediaTransportState(StrEnum):
    IDLE = "idle"
    PRECONNECTING = "preconnecting"
    PRECONNECTED = "preconnected"
    OPEN = "open"
    SWITCHING = "switching"
    CLOSED = "closed"


class MediaTransportEvent(StrEnum):
    CONNECTED = "connected"
    DEGRADED = "degraded"
    FAILED = "failed"
    STATS = "stats"


@dataclass(frozen=True)
class TransportStats:
    round_trip_time_ms: float = 0.0
    packet_loss_rate: float = 0.0
    bitrate_kbps: float = 0.0
    timestamp_ms: int = 0


@dataclass
class MediaTransportConfig:
    video_width: int = 640
    video_height: int = 360
    max_bitrate_kbps: int = 500
    ice_servers: list[dict[str, Any]] = field(default_factory=list)


class MediaTransport(ABC):
    @property
    @abstractmethod
    def state(self) -> MediaTransportState: ...

    @abstractmethod
    def preconnect(
        self,
        room_id: str,
        partner_id: str,
        config: MediaTransportConfig | dict[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def switch_partner(
        self,
        new_room_id: str,
        new_partner_id: str,
        config: MediaTransportConfig | dict[str, Any] | None = None,
    ) -> None: ...

    @abstractmethod
    def leave(self) -> None: ...

    @abstractmethod
    def on(
        self,
        event: MediaTransportEvent | str,
        callback: Callable[..., Any],
    ) -> None: ...

    @abstractmethod
    def off(
        self,
        event: MediaTransportEvent | str,
        callback: Callable[..., Any],
    ) -> None: ...

    @abstractmethod
    def emit(
        self,
        event: MediaTransportEvent | str,
        *args: Any,
        **kwargs: Any,
    ) -> None: ...


class FakeMediaTransport(MediaTransport):
    def __init__(self) -> None:
        self._state: MediaTransportState = MediaTransportState.IDLE
        self._listeners: dict[str, list[Callable[..., Any]]] = {
            ev.value: [] for ev in MediaTransportEvent
        }
        self.call_history: list[str] = []
        self.current_room_id: str | None = None
        self.current_partner_id: str | None = None
        self.audio_muted: bool = True
        self.video_muted: bool = True

    @property
    def state(self) -> MediaTransportState:
        return self._state

    def preconnect(
        self,
        room_id: str,
        partner_id: str,
        config: MediaTransportConfig | dict[str, Any] | None = None,
    ) -> None:
        self.call_history.append("preconnect")
        self._state = MediaTransportState.PRECONNECTING
        self.current_room_id = room_id
        self.current_partner_id = partner_id
        self.audio_muted = True
        self.video_muted = True
        self._state = MediaTransportState.PRECONNECTED

    def open(self) -> None:
        self.call_history.append("open")
        self.audio_muted = False
        self.video_muted = False
        self._state = MediaTransportState.OPEN
        self.emit(
            MediaTransportEvent.CONNECTED,
            room_id=self.current_room_id,
            partner_id=self.current_partner_id,
        )

    def switch_partner(
        self,
        new_room_id: str,
        new_partner_id: str,
        config: MediaTransportConfig | dict[str, Any] | None = None,
    ) -> None:
        self.call_history.append("switch_partner")
        self._state = MediaTransportState.SWITCHING
        self.current_room_id = new_room_id
        self.current_partner_id = new_partner_id
        self.audio_muted = False
        self.video_muted = False
        self._state = MediaTransportState.OPEN
        self.emit(
            MediaTransportEvent.CONNECTED,
            room_id=new_room_id,
            partner_id=new_partner_id,
        )

    def leave(self) -> None:
        self.call_history.append("leave")
        self.audio_muted = True
        self.video_muted = True
        self.current_room_id = None
        self.current_partner_id = None
        self._state = MediaTransportState.CLOSED

    def on(
        self,
        event: MediaTransportEvent | str,
        callback: Callable[..., Any],
    ) -> None:
        key = str(event)
        if key not in self._listeners:
            self._listeners[key] = []
        self._listeners[key].append(callback)

    def off(
        self,
        event: MediaTransportEvent | str,
        callback: Callable[..., Any],
    ) -> None:
        key = str(event)
        if key in self._listeners and callback in self._listeners[key]:
            self._listeners[key].remove(callback)

    def emit(
        self,
        event: MediaTransportEvent | str,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        key = str(event)
        for listener in list(self._listeners.get(key, [])):
            listener(*args, **kwargs)


class RoundMediaSession:
    def __init__(self, transport: MediaTransport) -> None:
        self.transport = transport
        self.is_connected = False
        self.is_degraded = False
        self.last_failure: str | None = None
        self.stats_report: TransportStats | None = None

        self.transport.on(MediaTransportEvent.CONNECTED, self._on_connected)
        self.transport.on(MediaTransportEvent.DEGRADED, self._on_degraded)
        self.transport.on(MediaTransportEvent.FAILED, self._on_failed)
        self.transport.on(MediaTransportEvent.STATS, self._on_stats)

    def _on_connected(self, **kwargs: Any) -> None:
        self.is_connected = True
        self.is_degraded = False

    def _on_degraded(self, **kwargs: Any) -> None:
        self.is_degraded = True

    def _on_failed(self, reason: str = "", **kwargs: Any) -> None:
        self.is_connected = False
        self.last_failure = reason

    def _on_stats(self, stats: TransportStats, **kwargs: Any) -> None:
        self.stats_report = stats

    def prepare_next_round(self, room_id: str, partner_id: str) -> None:
        self.transport.preconnect(room_id=room_id, partner_id=partner_id)

    def start_round(self) -> None:
        self.transport.open()

    def advance_to_partner(self, next_room_id: str, next_partner_id: str) -> None:
        self.transport.switch_partner(next_room_id, next_partner_id)

    def terminate_session(self) -> None:
        self.transport.leave()
