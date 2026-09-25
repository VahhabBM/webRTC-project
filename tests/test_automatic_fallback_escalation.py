"""T-39 automatic pair-scoped escalation onto the existing T-38 fallback."""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings

from apps.protocol import (
    ADAPTER_KIND_DIRECT,
    ADAPTER_KIND_RELAY,
    DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS,
    DirectMediaTransport,
    FailoverMediaTransport,
    MediaTransportEvent,
    MediaTransportState,
    RoundMediaSession,
    SharedLocalMedia,
    resolve_escalation_timeout_ms,
)

REPO = Path(__file__).resolve().parents[1]
CALL_ROOM_JS = REPO / "apps" / "events" / "static" / "events" / "js" / "call_room.js"
MEDIA_TRANSPORT_JS = (
    REPO / "apps" / "events" / "static" / "events" / "js" / "media_transport.js"
)
MEDIA_TRANSPORT_PY = REPO / "apps" / "protocol" / "media_transport.py"
VIDEO_ROOM_HTML = REPO / "apps" / "events" / "templates" / "events" / "video_room.html"
VIEWS_PY = REPO / "apps" / "events" / "views.py"
SETTINGS_PY = REPO / "config" / "settings" / "base.py"
NODE_TEST = REPO / "tests" / "js" / "test_automatic_fallback_escalation.mjs"
T38_NODE_TEST = REPO / "tests" / "js" / "test_second_media_adapter.mjs"


class ManualScheduler:
    def __init__(self) -> None:
        self.jobs: list[dict[str, Any]] = []

    def schedule(
        self, delay_ms: int, callback: Callable[[], None]
    ) -> Callable[[], None]:
        job = {"delay_ms": delay_ms, "callback": callback, "cancelled": False}
        self.jobs.append(job)

        def cancel() -> None:
            job["cancelled"] = True

        return cancel

    @property
    def live(self) -> list[dict[str, Any]]:
        return [job for job in self.jobs if not job["cancelled"]]

    def fire_pending(self) -> None:
        for job in list(self.jobs):
            if not job["cancelled"]:
                job["cancelled"] = True
                job["callback"]()


class DelayedConnectTransport(DirectMediaTransport):
    """Primary adapter that stays unconnected until complete_connect()."""

    def open(self) -> None:
        self.call_history.append("open")
        self.audio_muted = False
        self.video_muted = False
        self._state = MediaTransportState.OPEN

    def switch_partner(
        self,
        new_room_id: str,
        new_partner_id: str,
        config: object | None = None,
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

    def complete_connect(self) -> None:
        self.emit(
            MediaTransportEvent.CONNECTED,
            room_id=self.current_room_id,
            partner_id=self.current_partner_id,
        )


def _transport(
    *,
    selected_room_id: str = "test-room-101",
    room_id: str = "test-room-101",
    partner_id: str = "partner-a",
    escalation_timeout_ms: int | None = None,
    open_call: bool = True,
) -> tuple[FailoverMediaTransport, SharedLocalMedia, ManualScheduler]:
    media = SharedLocalMedia()
    sched = ManualScheduler()
    transport = FailoverMediaTransport(
        primary=DelayedConnectTransport(shared_media=media),
        shared_media=media,
        selected_room_id=selected_room_id,
        escalation_timeout_ms=escalation_timeout_ms,
        schedule_timer=sched.schedule,
    )
    transport.preconnect(room_id, partner_id)
    if open_call:
        transport.open()
    return transport, media, sched


def test_default_escalation_timeout_is_6000_and_defined_once():
    assert DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS == 6000
    assert settings.MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS == 6000
    assert resolve_escalation_timeout_ms() == 6000
    transport = FailoverMediaTransport()
    assert transport.escalation_timeout_ms == 6000

    py_src = MEDIA_TRANSPORT_PY.read_text(encoding="utf-8")
    js_src = MEDIA_TRANSPORT_JS.read_text(encoding="utf-8")
    assert py_src.count("6000") == 1
    assert "DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS = 6000" in py_src
    assert js_src.count("6000") == 1
    assert "DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS = 6000" in js_src
    assert "6000" not in CALL_ROOM_JS.read_text(encoding="utf-8")
    assert "6000" not in SETTINGS_PY.read_text(encoding="utf-8")


def test_escalation_timeout_is_configurable(settings):
    settings.MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS = 2500
    assert FailoverMediaTransport().escalation_timeout_ms == 2500

    sched = ManualScheduler()
    media = SharedLocalMedia()
    transport = FailoverMediaTransport(
        primary=DelayedConnectTransport(shared_media=media),
        shared_media=media,
        selected_room_id="test-room-101",
        escalation_timeout_ms=50,
        schedule_timer=sched.schedule,
    )
    transport.preconnect("test-room-101", "partner-a")
    transport.open()
    assert transport.escalation_timeout_ms == 50
    assert len(sched.live) == 1
    assert sched.live[0]["delay_ms"] == 50


def test_timer_starts_only_when_call_is_ready_and_cancels_on_connect():
    transport, _media, sched = _transport(open_call=False)
    assert sched.live == []

    transport.open()
    assert len(sched.live) == 1
    stale = sched.live[0]["callback"]

    transport._active.complete_connect()
    assert sched.live == []
    stale()
    assert transport.using_fallback is False
    assert transport.kind == ADAPTER_KIND_DIRECT
    assert transport.fallback_activations == 0


def test_automatic_fallback_after_timeout_reuses_media(caplog):
    caplog.set_level(logging.INFO)
    transport, media, sched = _transport()
    stream_id = transport.local_stream_id
    tracks = list(transport.attached_track_ids)
    old_peer = transport._active.peer_connection_id
    bound_before = list(transport._bound)

    sched.fire_pending()

    assert transport.using_fallback is True
    assert transport.kind == ADAPTER_KIND_RELAY
    assert transport.current_room_id == "test-room-101"
    assert transport.current_partner_id == "partner-a"
    assert media.get_user_media_calls == 1
    assert media.tracks_live is True
    assert transport.local_stream_id == stream_id
    assert transport.attached_track_ids == tracks
    assert transport._primary.peer_connection_id is None
    assert old_peer in transport._primary.abandoned_peer_ids
    assert transport._active.peer_connection_id != old_peer
    assert len(transport._bound) == len(bound_before)
    assert transport.last_escalation_log is not None
    assert transport.last_escalation_log["result"] == "activated"
    assert transport.last_escalation_log["timeout_ms"] == 6000
    assert transport.last_escalation_log["room_id"] == "test-room-101"
    assert "media_escalation" in caplog.text
    assert "token" not in caplog.text.lower()
    assert "secret" not in caplog.text.lower()


def test_no_escalation_when_fallback_unavailable(caplog):
    caplog.set_level(logging.INFO)
    failures: list[str] = []
    degraded: list[str] = []
    transport, media, sched = _transport(selected_room_id="")
    transport.on(
        MediaTransportEvent.FAILED,
        lambda reason="", **kwargs: failures.append(reason),
    )
    transport.on(
        MediaTransportEvent.DEGRADED,
        lambda reason="", **kwargs: degraded.append(reason),
    )

    sched.fire_pending()

    assert transport.using_fallback is False
    assert transport.kind == ADAPTER_KIND_DIRECT
    assert transport.fallback_activations == 0
    assert failures == ["ESCALATION_FALLBACK_UNAVAILABLE"]
    assert degraded == ["ESCALATION_FALLBACK_UNAVAILABLE"]
    assert media.get_user_media_calls == 1
    assert transport.last_escalation_log["result"] == "skipped"
    assert transport.last_escalation_log["reason"] == "FALLBACK_UNAVAILABLE"
    assert transport.last_escalation_log["fallback_configured"] is False
    assert "media_escalation skipped" in caplog.text
    assert "token" not in caplog.text.lower()


def test_pair_isolation_timeout_does_not_affect_other_pair():
    selected, selected_media, selected_sched = _transport(
        selected_room_id="test-room-101",
        room_id="test-room-101",
        partner_id="partner-a",
    )
    other, other_media, other_sched = _transport(
        selected_room_id="",
        room_id="test-room-202",
        partner_id="partner-c",
        escalation_timeout_ms=50,
    )

    selected_sched.fire_pending()

    assert selected.using_fallback is True
    assert selected.kind == ADAPTER_KIND_RELAY
    assert selected.current_room_id == "test-room-101"
    assert other.using_fallback is False
    assert other.kind == ADAPTER_KIND_DIRECT
    assert other.current_room_id == "test-room-202"
    assert other.current_partner_id == "partner-c"
    assert len(other_sched.live) == 1
    assert selected_media.get_user_media_calls == 1
    assert other_media.get_user_media_calls == 1


def test_stale_timer_ignored_after_partner_switch_leave_and_explicit_fallback():
    transport, _media, sched = _transport()
    first = sched.live[0]["callback"]

    transport.switch_partner("test-room-101", "partner-b")
    assert len(sched.live) == 1
    second = sched.live[0]["callback"]
    first()
    assert transport.using_fallback is False
    assert transport.current_partner_id == "partner-b"

    second()
    assert transport.using_fallback is True
    assert transport.fallback_activations == 1
    assert transport.current_partner_id == "partner-b"

    transport2, _media2, sched2 = _transport()
    leave_cb = sched2.live[0]["callback"]
    transport2.leave()
    leave_cb()
    assert transport2.using_fallback is False
    assert transport2.state == MediaTransportState.CLOSED

    transport3, _media3, sched3 = _transport()
    force_cb = sched3.live[0]["callback"]
    assert transport3.force_fallback() is True
    assert transport3.fallback_activations == 1
    force_cb()
    assert transport3.fallback_activations == 1
    assert transport3.using_fallback is True


def test_explicit_t38_force_fallback_still_works():
    media = SharedLocalMedia()
    session = RoundMediaSession(
        FailoverMediaTransport(shared_media=media, selected_room_id="test-room-101")
    )
    session.prepare_next_round("test-room-101", "partner-a")
    session.start_round()
    assert session.is_connected is True
    assert session.transport.kind == ADAPTER_KIND_DIRECT

    assert session.transport.force_fallback() is True

    assert session.transport.using_fallback is True
    assert session.transport.kind == ADAPTER_KIND_RELAY
    assert session.is_connected is True
    assert media.get_user_media_calls == 1


def test_views_and_template_inject_escalation_timeout():
    src = VIEWS_PY.read_text(encoding="utf-8")
    html = VIDEO_ROOM_HTML.read_text(encoding="utf-8")
    assert "MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS" in src
    assert "media_fallback_escalation_timeout_ms" in src
    assert "mediaFallbackEscalationTimeoutMs" in html
    assert "escalationTimeoutMs" in html
    assert "force_media_fallback" in html
    assert "__t38ForceMediaFallback" in html


def test_escalation_log_has_path_details_without_secrets():
    transport, _media, sched = _transport()
    sched.fire_pending()
    log = transport.last_escalation_log
    assert log is not None
    for key in (
        "room_id",
        "partner_id",
        "primary_path",
        "timeout_ms",
        "fallback_configured",
        "fallback_selected_room_id",
        "fallback_allowed",
        "result",
        "reason",
    ):
        assert key in log
    blob = str(log).lower()
    assert "token" not in blob
    assert "secret" not in blob
    assert "password" not in blob
    assert "ice_servers" not in blob
    assert "credential" not in blob


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for JS adapter tests"
)
def test_node_automatic_fallback_escalation():
    completed = subprocess.run(
        ["node", "--test", str(NODE_TEST)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


@pytest.mark.skipif(
    shutil.which("node") is None, reason="node is required for JS adapter tests"
)
def test_node_t38_explicit_fallback_regression():
    completed = subprocess.run(
        ["node", "--test", str(T38_NODE_TEST)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
