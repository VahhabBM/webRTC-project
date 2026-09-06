import pytest

from apps.protocol import (
    FakeMediaTransport,
    MediaTransport,
    MediaTransportEvent,
    MediaTransportState,
    RoundMediaSession,
    TransportStats,
)


class TestMediaTransportInterface:
    def test_media_transport_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            MediaTransport()

    def test_fake_implementation_lifecycle_transitions(self):
        transport = FakeMediaTransport()
        assert transport.state == MediaTransportState.IDLE

        transport.preconnect("room-101", "user-200")
        assert transport.state == MediaTransportState.PRECONNECTED
        assert transport.audio_muted is True
        assert transport.video_muted is True

        transport.open()
        assert transport.state == MediaTransportState.OPEN
        assert transport.audio_muted is False
        assert transport.video_muted is False

        transport.switch_partner("room-102", "user-300")
        assert transport.current_partner_id == "user-300"
        assert transport.state == MediaTransportState.OPEN

        transport.leave()
        assert transport.state == MediaTransportState.CLOSED
        assert transport.audio_muted is True

        assert transport.call_history == [
            "preconnect",
            "open",
            "switch_partner",
            "leave",
        ]

    def test_events_registration_and_dispatch(self):
        transport = FakeMediaTransport()
        dispatched_events: list[str] = []

        def on_connected(**kwargs):
            dispatched_events.append("connected")

        def on_degraded(**kwargs):
            dispatched_events.append("degraded")

        def on_failed(reason: str, **kwargs):
            dispatched_events.append(f"failed:{reason}")

        def on_stats(stats: TransportStats, **kwargs):
            dispatched_events.append(f"stats:{stats.bitrate_kbps}")

        transport.on(MediaTransportEvent.CONNECTED, on_connected)
        transport.on(MediaTransportEvent.DEGRADED, on_degraded)
        transport.on(MediaTransportEvent.FAILED, on_failed)
        transport.on(MediaTransportEvent.STATS, on_stats)

        transport.emit(MediaTransportEvent.CONNECTED)
        transport.emit(MediaTransportEvent.DEGRADED)
        transport.emit(MediaTransportEvent.FAILED, reason="ICE_TIMEOUT")
        transport.emit(
            MediaTransportEvent.STATS,
            stats=TransportStats(bitrate_kbps=480.5),
        )

        assert dispatched_events == [
            "connected",
            "degraded",
            "failed:ICE_TIMEOUT",
            "stats:480.5",
        ]

        transport.off(MediaTransportEvent.CONNECTED, on_connected)
        transport.emit(MediaTransportEvent.CONNECTED)
        assert dispatched_events.count("connected") == 1


class TestCallerDecoupling:
    def test_caller_drives_session_via_interface_only(self):
        transport = FakeMediaTransport()
        session = RoundMediaSession(transport)

        session.prepare_next_round("room-abc", "partner-xyz")
        assert transport.state == MediaTransportState.PRECONNECTED
        assert not session.is_connected

        session.start_round()
        assert session.is_connected
        assert transport.state == MediaTransportState.OPEN

        transport.emit(MediaTransportEvent.DEGRADED)
        assert session.is_degraded is True

        session.advance_to_partner("room-def", "partner-uvw")
        assert transport.current_partner_id == "partner-uvw"

        transport.emit(MediaTransportEvent.FAILED, reason="NETWORK_DISCONNECTED")
        assert session.is_connected is False
        assert session.last_failure == "NETWORK_DISCONNECTED"

        session.terminate_session()
        assert transport.state == MediaTransportState.CLOSED

    def test_caller_swappable_with_alternative_transport(self):
        class SecondaryBackupTransport(MediaTransport):
            def __init__(self):
                self._state = MediaTransportState.IDLE
                self.invocations: list[str] = []
                self.listeners: dict[str, list] = {}

            @property
            def state(self) -> MediaTransportState:
                return self._state

            def preconnect(self, room_id, partner_id, config=None):
                self.invocations.append("secondary_preconnect")
                self._state = MediaTransportState.PRECONNECTED

            def open(self):
                self.invocations.append("secondary_open")
                self._state = MediaTransportState.OPEN
                self.emit(MediaTransportEvent.CONNECTED)

            def switch_partner(self, new_room_id, new_partner_id, config=None):
                self.invocations.append("secondary_switch")

            def leave(self):
                self.invocations.append("secondary_leave")
                self._state = MediaTransportState.CLOSED

            def on(self, event, callback):
                self.listeners.setdefault(str(event), []).append(callback)

            def off(self, event, callback):
                pass

            def emit(self, event, *args, **kwargs):
                for cb in self.listeners.get(str(event), []):
                    cb(*args, **kwargs)

        backup_transport = SecondaryBackupTransport()
        session = RoundMediaSession(backup_transport)

        session.prepare_next_round("r1", "p1")
        session.start_round()
        session.terminate_session()

        assert session.is_connected is True
        assert backup_transport.invocations == [
            "secondary_preconnect",
            "secondary_open",
            "secondary_leave",
        ]
