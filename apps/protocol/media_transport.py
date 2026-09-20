from __future__ import annotations

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


ADAPTER_KIND_DIRECT = "direct"
ADAPTER_KIND_RELAY = "relay"


def fallback_allowed_for_room(
    room_id: str | None,
    selected_room_id: str | None,
) -> bool:
    """True only when a test/feature key selects this exact pair room."""
    if not selected_room_id or not room_id:
        return False
    return str(room_id) == str(selected_room_id)


class SharedLocalMedia:
    """Owns one local stream/track set. Adapters attach; they never re-acquire."""

    def __init__(self, stream_id: str = "local-stream") -> None:
        self.stream_id = stream_id
        self.audio_track_id = f"{stream_id}-audio"
        self.video_track_id = f"{stream_id}-video"
        self.get_user_media_calls = 0
        self.tracks_live = False
        self._acquired = False

    def acquire(self) -> SharedLocalMedia:
        if self._acquired and self.tracks_live:
            return self
        self.get_user_media_calls += 1
        self._acquired = True
        self.tracks_live = True
        return self

    def release(self) -> None:
        self.tracks_live = False
        self._acquired = False


class _LifecycleAdapter(MediaTransport):
    """In-memory T-26 adapter with explicit peer teardown and shared media."""

    kind: str = ADAPTER_KIND_DIRECT

    def __init__(self, shared_media: SharedLocalMedia | None = None) -> None:
        self._shared_media = shared_media or SharedLocalMedia()
        self._state: MediaTransportState = MediaTransportState.IDLE
        self._listeners: dict[str, list[Callable[..., Any]]] = {
            ev.value: [] for ev in MediaTransportEvent
        }
        self.call_history: list[str] = []
        self.current_room_id: str | None = None
        self.current_partner_id: str | None = None
        self.audio_muted: bool = True
        self.video_muted: bool = True
        self.peer_generation = 0
        self.peer_connection_id: str | None = None
        self.abandoned_peer_ids: list[str] = []
        self.attached_track_ids: list[str] = []
        self.local_stream_id: str | None = None

    @property
    def state(self) -> MediaTransportState:
        return self._state

    def _replace_peer(self) -> None:
        if self.peer_connection_id is not None:
            self.abandoned_peer_ids.append(self.peer_connection_id)
        self.peer_generation += 1
        self.peer_connection_id = f"{self.kind}-pc-{self.peer_generation}"

    def _attach_shared_tracks(self) -> None:
        media = self._shared_media.acquire()
        self.local_stream_id = media.stream_id
        self.attached_track_ids = [media.audio_track_id, media.video_track_id]

    def abandon(self) -> None:
        """Close this adapter's peer without releasing shared camera/mic tracks."""
        if self.peer_connection_id is not None:
            self.abandoned_peer_ids.append(self.peer_connection_id)
        self.peer_connection_id = None
        self.attached_track_ids = []
        self._listeners = {ev.value: [] for ev in MediaTransportEvent}
        self._state = MediaTransportState.CLOSED

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
        self._replace_peer()
        self._attach_shared_tracks()
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
        self._replace_peer()
        self._attach_shared_tracks()
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
        self.abandon()

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


class DirectMediaTransport(_LifecycleAdapter):
    """Primary/direct WebRTC adapter. Default production path."""

    kind = ADAPTER_KIND_DIRECT


class RelayMediaTransport(_LifecycleAdapter):
    """Second adapter: TURN-relay-only path behind the same T-26 contract."""

    kind = ADAPTER_KIND_RELAY


class FailoverMediaTransport(MediaTransport):
    """Pair-scoped primary→fallback switch. Callers depend only on MediaTransport."""

    def __init__(
        self,
        *,
        primary: MediaTransport | None = None,
        fallback_factory: Callable[[], MediaTransport] | None = None,
        selected_room_id: str | None = None,
        shared_media: SharedLocalMedia | None = None,
    ) -> None:
        self._shared_media = shared_media or SharedLocalMedia()
        self._selected_room_id = selected_room_id or ""
        self._fallback_factory = fallback_factory or (
            lambda: RelayMediaTransport(shared_media=self._shared_media)
        )
        self._primary = primary or DirectMediaTransport(shared_media=self._shared_media)
        self._active: MediaTransport = self._primary
        self._using_fallback = False
        self._switching = False
        self._listeners: dict[str, list[Callable[..., Any]]] = {
            ev.value: [] for ev in MediaTransportEvent
        }
        self._bound: list[tuple[str, Callable[..., Any]]] = []
        self.fallback_activations = 0
        self._bind_active()

    @property
    def state(self) -> MediaTransportState:
        return self._active.state

    @property
    def kind(self) -> str:
        return getattr(self._active, "kind", ADAPTER_KIND_DIRECT)

    @property
    def using_fallback(self) -> bool:
        return self._using_fallback

    @property
    def current_room_id(self) -> str | None:
        return getattr(self._active, "current_room_id", None)

    @property
    def current_partner_id(self) -> str | None:
        return getattr(self._active, "current_partner_id", None)

    @property
    def call_history(self) -> list[str]:
        return list(getattr(self._active, "call_history", []))

    @property
    def audio_muted(self) -> bool:
        return bool(getattr(self._active, "audio_muted", False))

    @property
    def video_muted(self) -> bool:
        return bool(getattr(self._active, "video_muted", False))

    @property
    def local_stream_id(self) -> str | None:
        return getattr(self._active, "local_stream_id", self._shared_media.stream_id)

    @property
    def attached_track_ids(self) -> list[str]:
        return list(getattr(self._active, "attached_track_ids", []))

    def _bind_active(self) -> None:
        self._bound = []
        for event in MediaTransportEvent:
            handler = self._make_forwarder(event)
            self._bound.append((event.value, handler))
            self._active.on(event, handler)

    def _unbind_active(self) -> None:
        for key, handler in self._bound:
            self._active.off(key, handler)
        self._bound = []

    def _make_forwarder(self, event: MediaTransportEvent) -> Callable[..., Any]:
        def handler(*args: Any, **kwargs: Any) -> None:
            if event == MediaTransportEvent.FAILED:
                self._on_active_failed(*args, **kwargs)
                return
            self.emit(event, *args, **kwargs)

        return handler

    def _close_active_keep_media(self) -> None:
        self._unbind_active()
        abandon = getattr(self._active, "abandon", None)
        if callable(abandon):
            abandon()

    def _activate_fallback(self, reason: str = "") -> bool:
        if self._switching or self._using_fallback:
            return False
        room_id = self.current_room_id
        partner_id = self.current_partner_id
        if not fallback_allowed_for_room(room_id, self._selected_room_id):
            return False
        was_open = self._active.state == MediaTransportState.OPEN
        self._switching = True
        self.emit(MediaTransportEvent.DEGRADED, reason=reason or "PRIMARY_FAILED")
        self._close_active_keep_media()
        self._active = self._fallback_factory()
        self._using_fallback = True
        self.fallback_activations += 1
        self._bind_active()
        if room_id and partner_id:
            self._active.preconnect(room_id, partner_id)
            if was_open:
                self._active.open()
        self._switching = False
        return True

    def _restore_primary_if_needed(self, new_room_id: str) -> None:
        if not self._using_fallback:
            return
        if fallback_allowed_for_room(new_room_id, self._selected_room_id):
            return
        self._close_active_keep_media()
        self._active = self._primary
        self._using_fallback = False
        self._bind_active()

    def _on_active_failed(self, reason: str = "", **kwargs: Any) -> None:
        if self._activate_fallback(reason):
            return
        self.emit(MediaTransportEvent.FAILED, reason=reason, **kwargs)

    def force_fallback(self) -> bool:
        """Deterministic test/feature trigger. No-op unless this pair is selected."""
        return self._activate_fallback("TEST_TRIGGER")

    def preconnect(
        self,
        room_id: str,
        partner_id: str,
        config: MediaTransportConfig | dict[str, Any] | None = None,
    ) -> None:
        self._restore_primary_if_needed(room_id)
        self._active.preconnect(room_id, partner_id, config)

    def open(self) -> None:
        self._active.open()

    def switch_partner(
        self,
        new_room_id: str,
        new_partner_id: str,
        config: MediaTransportConfig | dict[str, Any] | None = None,
    ) -> None:
        self._restore_primary_if_needed(new_room_id)
        self._active.switch_partner(new_room_id, new_partner_id, config)

    def leave(self) -> None:
        self._unbind_active()
        self._active.leave()
        self._shared_media.release()
        self._using_fallback = False

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
