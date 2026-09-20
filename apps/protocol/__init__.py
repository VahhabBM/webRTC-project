"""Protocol contract for the WebRTC Event Platform."""

from .media_transport import (
    ADAPTER_KIND_DIRECT,
    ADAPTER_KIND_RELAY,
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
)

__all__ = [
    "ADAPTER_KIND_DIRECT",
    "ADAPTER_KIND_RELAY",
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
]
