"""Protocol contract for the WebRTC Event Platform."""

from .media_transport import (
    ADAPTER_KIND_DIRECT,
    ADAPTER_KIND_RELAY,
    DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS,
    DirectMediaTransport,
    FailoverMediaTransport,
    FakeMediaTransport,
    MediaTransport,
    MediaTransportConfig,
    MediaTransportEvent,
    MediaTransportState,
    RelayMediaTransport,
    RoundMediaSession,
    SharedLocalMedia,
    TransportStats,
    fallback_allowed_for_room,
    resolve_escalation_timeout_ms,
)

__all__ = [
    "ADAPTER_KIND_DIRECT",
    "ADAPTER_KIND_RELAY",
    "DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS",
    "DirectMediaTransport",
    "FailoverMediaTransport",
    "FakeMediaTransport",
    "MediaTransport",
    "MediaTransportConfig",
    "MediaTransportEvent",
    "MediaTransportState",
    "RelayMediaTransport",
    "RoundMediaSession",
    "SharedLocalMedia",
    "TransportStats",
    "fallback_allowed_for_room",
    "resolve_escalation_timeout_ms",
]
