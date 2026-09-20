"""T-38 second media adapter behind the T-26 contract."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from django.conf import settings

from apps.protocol import (
    ADAPTER_KIND_DIRECT,
    ADAPTER_KIND_RELAY,
    DirectMediaTransport,
    FailoverMediaTransport,
    FakeMediaTransport,
    MediaTransport,
    MediaTransportEvent,
    MediaTransportState,
    RelayMediaTransport,
    RoundMediaSession,
    SharedLocalMedia,
    fallback_allowed_for_room,
)

REPO = Path(__file__).resolve().parents[1]
CALL_ROOM_JS = REPO / "apps" / "events" / "static" / "events" / "js" / "call_room.js"
MEDIA_TRANSPORT_JS = (
    REPO / "apps" / "events" / "static" / "events" / "js" / "media_transport.js"
)
NEGOTIATOR_JS = (
    REPO / "apps" / "events" / "static" / "events" / "js" / "perfect_negotiator.js"
)
NEGOTIATOR_JS_COPY = REPO / "apps" / "events" / "js" / "perfect_negotiator.js"
VIDEO_ROOM_HTML = REPO / "apps" / "events" / "templates" / "events" / "video_room.html"
CREATE_TEST_PAIR = (
    REPO / "apps" / "events" / "management" / "commands" / "create_test_pair.py"
)
VIEWS_PY = REPO / "apps" / "events" / "views.py"
NODE_TEST = REPO / "tests" / "js" / "test_second_media_adapter.mjs"


def test_fallback_room_helper_is_pair_scoped():
    assert fallback_allowed_for_room("test-room-101", "test-room-101") is True
    assert fallback_allowed_for_room("test-room-202", "test-room-101") is False
    assert fallback_allowed_for_room("test-room-101", "") is False
    assert fallback_allowed_for_room("test-room-101", None) is False
    assert fallback_allowed_for_room(None, "test-room-101") is False


def test_fallback_flag_defaults_off():
    assert getattr(settings, "MEDIA_FALLBACK_ROOM_ID", "") == ""


def test_second_adapter_implements_t26_contract():
    assert issubclass(DirectMediaTransport, MediaTransport)
    assert issubclass(RelayMediaTransport, MediaTransport)
    assert issubclass(FailoverMediaTransport, MediaTransport)
    with pytest.raises(TypeError):
        MediaTransport()


def test_relay_lifecycle_matches_t26_session():
    transport = RelayMediaTransport()
    session = RoundMediaSession(transport)
    session.prepare_next_round("room-1", "partner-1")
    assert transport.state == MediaTransportState.PRECONNECTED
    session.start_round()
    assert session.is_connected is True
    assert transport.state == MediaTransportState.OPEN
    session.advance_to_partner("room-2", "partner-2")
    assert transport.current_partner_id == "partner-2"
    session.terminate_session()
    assert transport.state == MediaTransportState.CLOSED


def test_primary_path_does_not_switch_without_flag():
    media = SharedLocalMedia()
    transport = FailoverMediaTransport(shared_media=media, selected_room_id="")
    session = RoundMediaSession(transport)
    session.prepare_next_round("test-room-101", "partner-a")
    session.start_round()
    assert session.is_connected is True
    assert transport.kind == ADAPTER_KIND_DIRECT

    transport._active.emit(MediaTransportEvent.FAILED, reason="ICE_CONNECTION_FAILED")

    assert transport.using_fallback is False
    assert transport.fallback_activations == 0
    assert transport.kind == ADAPTER_KIND_DIRECT
    assert session.is_connected is False
    assert session.last_failure == "ICE_CONNECTION_FAILED"
    assert media.get_user_media_calls == 1


def test_fallback_activates_for_selected_pair_only():
    selected_media = SharedLocalMedia()
    other_media = SharedLocalMedia()
    selected = FailoverMediaTransport(
        shared_media=selected_media, selected_room_id="test-room-101"
    )
    other = FailoverMediaTransport(shared_media=other_media, selected_room_id="")

    selected.preconnect("test-room-101", "partner-a")
    selected.open()
    other.preconnect("test-room-202", "partner-c")
    other.open()

    selected._active.emit(MediaTransportEvent.FAILED, reason="ICE_CONNECTION_FAILED")
    other._active.emit(MediaTransportEvent.FAILED, reason="ICE_CONNECTION_FAILED")

    assert selected.using_fallback is True
    assert selected.kind == ADAPTER_KIND_RELAY
    assert selected.current_room_id == "test-room-101"
    assert selected.current_partner_id == "partner-a"
    assert other.using_fallback is False
    assert other.kind == ADAPTER_KIND_DIRECT
    assert other.current_room_id == "test-room-202"


def test_fallback_reuses_same_stream_and_tracks():
    media = SharedLocalMedia()
    transport = FailoverMediaTransport(
        shared_media=media, selected_room_id="test-room-101"
    )
    transport.preconnect("test-room-101", "partner-a")
    transport.open()
    stream_id = transport.local_stream_id
    tracks = list(transport.attached_track_ids)
    old_peer = transport._active.peer_connection_id

    assert transport.force_fallback() is True

    assert media.get_user_media_calls == 1
    assert media.tracks_live is True
    assert transport.local_stream_id == stream_id
    assert transport.attached_track_ids == tracks
    assert transport.kind == ADAPTER_KIND_RELAY
    assert transport._primary.peer_connection_id is None
    assert old_peer in transport._primary.abandoned_peer_ids
    assert transport._active.peer_connection_id != old_peer
    assert (
        transport._active.peer_connection_id
        not in transport._primary.abandoned_peer_ids
    )


def test_force_fallback_is_noop_for_other_rooms():
    transport = FailoverMediaTransport(selected_room_id="test-room-101")
    transport.preconnect("test-room-202", "partner-c")
    transport.open()
    assert transport.force_fallback() is False
    assert transport.using_fallback is False
    assert transport.kind == ADAPTER_KIND_DIRECT


def test_session_stays_on_same_pair_through_fallback():
    transport = FailoverMediaTransport(selected_room_id="test-room-101")
    session = RoundMediaSession(transport)
    session.prepare_next_round("test-room-101", "partner-a")
    session.start_round()
    assert session.is_connected is True

    assert transport.force_fallback() is True

    assert session.is_connected is True
    assert session.last_failure is None
    assert transport.current_room_id == "test-room-101"
    assert transport.current_partner_id == "partner-a"
    session.terminate_session()
    assert transport.state == MediaTransportState.CLOSED
    assert transport._shared_media.tracks_live is False


def test_partner_switch_to_other_room_leaves_fallback():
    transport = FailoverMediaTransport(selected_room_id="test-room-101")
    transport.preconnect("test-room-101", "partner-a")
    transport.open()
    transport.force_fallback()
    assert transport.using_fallback is True

    transport.switch_partner("test-room-202", "partner-c")

    assert transport.using_fallback is False
    assert transport.kind == ADAPTER_KIND_DIRECT
    assert transport.current_room_id == "test-room-202"
    assert transport.current_partner_id == "partner-c"


def test_leave_does_not_leave_stale_listeners_or_peers():
    media = SharedLocalMedia()
    transport = FailoverMediaTransport(
        shared_media=media, selected_room_id="test-room-101"
    )
    hits: list[str] = []
    transport.on(MediaTransportEvent.FAILED, lambda **kwargs: hits.append("failed"))
    transport.preconnect("test-room-101", "partner-a")
    transport.open()
    transport.force_fallback()
    stale_primary = transport._primary
    transport.leave()

    stale_primary.emit(MediaTransportEvent.FAILED, reason="STALE")
    assert hits == []
    assert stale_primary.peer_connection_id is None
    assert media.tracks_live is False


def test_fake_primary_path_still_works_unchanged():
    transport = FakeMediaTransport()
    session = RoundMediaSession(transport)
    session.prepare_next_round("room-abc", "partner-xyz")
    session.start_round()
    assert session.is_connected is True
    assert transport.state == MediaTransportState.OPEN
    session.terminate_session()
    assert transport.state == MediaTransportState.CLOSED
    assert transport.call_history == ["preconnect", "open", "leave"]


def test_call_room_depends_on_shared_interface_only():
    src = CALL_ROOM_JS.read_text(encoding="utf-8")
    assert "FailoverMediaTransport" in src
    assert 'from "./media_transport.js"' in src
    assert "PerfectNegotiator" not in src
    assert "createRelayMediaAdapter" not in src
    assert "iceTransportPolicy" not in src
    assert "navigator.mediaDevices.getUserMedia" not in src
    assert "forceMediaFallback" in src


def test_media_transport_js_hides_relay_factory():
    src = MEDIA_TRANSPORT_JS.read_text(encoding="utf-8")
    assert "function createRelayMediaAdapter" in src
    assert "export function createRelayMediaAdapter" not in src
    assert 'iceTransportPolicy: "relay"' in src
    assert "navigator.mediaDevices.getUserMedia" not in src
    assert "getUserMedia" not in src


def test_video_room_exposes_pair_scoped_test_switch():
    html = VIDEO_ROOM_HTML.read_text(encoding="utf-8")
    assert "media_fallback_room" in html
    assert "force_media_fallback" in html
    assert "mediaFallbackRoomId" in html
    assert "__t38ForceMediaFallback" in html


def test_views_inject_fallback_room_setting():
    src = VIEWS_PY.read_text(encoding="utf-8")
    assert "MEDIA_FALLBACK_ROOM_ID" in src
    assert "media_fallback_room_id" in src


def test_create_test_pair_can_make_an_isolation_pair():
    src = CREATE_TEST_PAIR.read_text(encoding="utf-8")
    assert "--extra-pair" in src
    assert "test-room-101" in src
    assert "test-room-202" in src


def test_negotiator_copies_stay_in_sync():
    assert NEGOTIATOR_JS.read_text(encoding="utf-8") == NEGOTIATOR_JS_COPY.read_text(
        encoding="utf-8"
    )


def test_getusermedia_still_confined_to_shared_acquire():
    src = NEGOTIATOR_JS.read_text(encoding="utf-8")
    assert src.count("navigator.mediaDevices.getUserMedia") == 1
    assert "detachPeerKeepMedia" in src


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for JS adapter tests"
)
def test_node_second_media_adapter():
    completed = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
