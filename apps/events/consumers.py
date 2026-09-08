"""Authenticated WebSocket connection layer (T-14 & T-27)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from datetime import UTC, datetime

from channels.db import database_sync_to_async
from channels.generic.websocket import AsyncWebsocketConsumer
from django.conf import settings

from apps.protocol.constants import (
    CLOSE_INTERNAL_ERROR,
    CLOSE_MESSAGE_TOO_BIG,
    CLOSE_NORMAL,
    ERROR_CLOSE_CODES,
    ErrorCode,
    MessageType,
)
from apps.protocol.exceptions import ProtocolError
from apps.protocol.schemas import (
    build_server_clock_sync,
    build_server_error,
    build_server_hello,
    build_server_ice_candidate,
    build_server_pong,
    build_server_webrtc_answer,
    build_server_webrtc_offer,
    error_from_protocol_error,
)
from apps.protocol.validators import validate_message

from .auth import resolve_participant_from_scope

logger = logging.getLogger(__name__)


def _timestamp_ms() -> int:
    return int(datetime.now(UTC).timestamp() * 1000)


@database_sync_to_async
def _participant_from_scope(scope):
    return resolve_participant_from_scope(scope)


@database_sync_to_async
def _resolve_partner_for_participant(participant, room_id: str):
    from django.db.models import Q

    from apps.events.models import Pair

    if not participant or not room_id:
        return None

    pair = (
        Pair.objects.filter(room_id=room_id)
        .filter(Q(participant_a=participant) | Q(participant_b=participant))
        .select_related("participant_a", "participant_b")
        .first()
    )
    if not pair:
        return None
    return (
        pair.participant_b
        if pair.participant_a_id == participant.pk
        else pair.participant_a
    )


class ParticipantConsumer(AsyncWebsocketConsumer):
    """One isolated, session-authenticated protocol connection."""

    async def connect(self):
        self.participant = None
        self.authenticated = False
        self.handshake_complete = False
        self.last_activity = time.monotonic()
        self._rate_timestamps = deque()
        self._heartbeat_task = None
        try:
            self.participant = await _participant_from_scope(self.scope)
            self.authenticated = self.participant is not None
        except Exception:
            logger.exception("Unexpected WebSocket session authentication failure")
            await self.accept()
            await self._fatal(ErrorCode.ERR_INTERNAL)
            return
        await self.accept()
        if not self.authenticated:
            await self._fatal(ErrorCode.ERR_NOT_AUTHENTICATED)
            return
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

    async def disconnect(self, close_code):
        task = self._heartbeat_task
        self._heartbeat_task = None
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if hasattr(self, "participant_group") and self.channel_layer:
            await self.channel_layer.group_discard(
                self.participant_group, self.channel_name
            )

    async def receive(self, text_data=None, bytes_data=None):
        self.last_activity = time.monotonic()
        raw = text_data if text_data is not None else bytes_data
        if raw is None:
            return
        max_bytes = getattr(settings, "WEBSOCKET_MAX_MESSAGE_BYTES", 64 * 1024)
        size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
        if size > max_bytes:
            await self._send_error(
                ErrorCode.ERR_INVALID_MESSAGE,
                "Message exceeds the maximum allowed size.",
            )
            await self.close(code=CLOSE_MESSAGE_TOO_BIG)
            return
        if not self._within_rate_limit():
            await self._fatal(ErrorCode.ERR_RATE_LIMITED)
            return
        try:
            msg_type, payload = validate_message(raw)
        except ProtocolError as exc:
            await self._send_protocol_error(exc)
            if exc.code in ERROR_CLOSE_CODES:
                await self.close(code=ERROR_CLOSE_CODES[exc.code])
            return
        except Exception:
            logger.exception("Unexpected protocol parsing failure")
            await self._fatal(ErrorCode.ERR_INTERNAL)
            return

        try:
            await self._handle_message(msg_type, payload)
        except Exception:
            logger.exception(
                "Unexpected WebSocket failure for participant %s",
                self.participant.pk,
            )
            await self._fatal(ErrorCode.ERR_INTERNAL)

    async def _handle_message(self, msg_type, payload):
        if not self.handshake_complete:
            if msg_type != MessageType.CLIENT_HELLO:
                await self._send_error(
                    ErrorCode.ERR_INVALID_STATE,
                    "client.hello is required before other messages.",
                    original_type=str(msg_type),
                )
                return
            await self._complete_handshake(payload)
            return

        if msg_type == MessageType.CLIENT_PING:
            await self._send(
                build_server_pong(
                    client_ts_echo=payload["client_ts"], server_ts=_timestamp_ms()
                )
            )
        elif msg_type == MessageType.CLIENT_CLOCK_SYNC:
            await self._send(
                build_server_clock_sync(
                    client_ts_echo=payload["client_ts"], server_ts=_timestamp_ms()
                )
            )
        elif msg_type == MessageType.CLIENT_HELLO:
            await self._send_error(
                ErrorCode.ERR_INVALID_STATE,
                "client.hello has already been accepted.",
                original_type=str(msg_type),
            )
        elif msg_type in (
            MessageType.CLIENT_WEBRTC_OFFER,
            MessageType.CLIENT_WEBRTC_ANSWER,
            MessageType.CLIENT_WEBRTC_ICE,
        ):
            await self._handle_webrtc_signal(msg_type, payload)
        else:
            await self._send_error(
                ErrorCode.ERR_INVALID_STATE,
                "This message is not available until pairing is implemented.",
                original_type=str(msg_type),
            )

    async def _complete_handshake(self, payload):
        self.handshake_complete = True
        if self.channel_layer:
            self.participant_group = f"participant_{self.participant.pk}"
            await self.channel_layer.group_add(
                self.participant_group, self.channel_name
            )
        await self._send(
            build_server_hello(
                participant_id=str(self.participant.pk),
                server_ts=_timestamp_ms(),
                client_ts_echo=payload["client_ts"],
                event_id=str(self.participant.event_id),
            )
        )

    async def _handle_webrtc_signal(self, msg_type: MessageType, payload: dict) -> None:
        room_id = payload.get("room_id")
        partner = await _resolve_partner_for_participant(self.participant, room_id)
        if not partner:
            await self._send_error(
                ErrorCode.ERR_WRONG_ROOM,
                f"Participant is not assigned to room {room_id}.",
                original_type=str(msg_type),
            )
            return

        from_id = str(self.participant.pk)
        if msg_type == MessageType.CLIENT_WEBRTC_OFFER:
            outbound = build_server_webrtc_offer(
                room_id=room_id,
                from_participant_id=from_id,
                sdp=payload["sdp"],
            )
        elif msg_type == MessageType.CLIENT_WEBRTC_ANSWER:
            outbound = build_server_webrtc_answer(
                room_id=room_id,
                from_participant_id=from_id,
                sdp=payload["sdp"],
            )
        elif msg_type == MessageType.CLIENT_WEBRTC_ICE:
            outbound = build_server_ice_candidate(
                room_id=room_id,
                from_participant_id=from_id,
                candidate=payload["candidate"],
                sdp_mid=payload["sdp_mid"],
                sdp_mline_index=payload["sdp_mline_index"],
            )
        else:
            return

        if self.channel_layer:
            await self.channel_layer.group_send(
                f"participant_{partner.pk}",
                {
                    "type": "webrtc_relay",
                    "message": outbound,
                },
            )

    async def webrtc_relay(self, event: dict) -> None:
        await self._send(event["message"])

    def _within_rate_limit(self) -> bool:
        now = time.monotonic()
        limit = getattr(settings, "PROTOCOL_RATE_LIMIT_MESSAGES_PER_MINUTE", 60)
        while self._rate_timestamps and self._rate_timestamps[0] <= now - 60:
            self._rate_timestamps.popleft()
        if len(self._rate_timestamps) >= limit:
            return False
        self._rate_timestamps.append(now)
        return True

    async def _heartbeat_loop(self):
        interval = getattr(settings, "WEBSOCKET_HEARTBEAT_INTERVAL_SECONDS", 30)
        timeout = getattr(settings, "WEBSOCKET_HEARTBEAT_TIMEOUT_SECONDS", 90)
        try:
            while True:
                await asyncio.sleep(interval)
                if time.monotonic() - self.last_activity > timeout:
                    await self.close(code=CLOSE_NORMAL)
                    return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Heartbeat failure for participant %s", self.participant.pk
            )

    async def _send(self, message):
        await self.send(text_data=json.dumps(message))

    async def _send_error(self, code, message, *, original_type=None, detail=None):
        await self._send(
            build_server_error(
                code=str(code),
                message=message,
                original_type=original_type,
                detail=detail,
            )
        )

    async def _send_protocol_error(self, exc: ProtocolError):
        await self._send(error_from_protocol_error(exc))

    async def _fatal(self, code: ErrorCode):
        messages = {
            ErrorCode.ERR_NOT_AUTHENTICATED: "Authentication is required.",
            ErrorCode.ERR_RATE_LIMITED: "Too many messages; connection closed.",
            ErrorCode.ERR_INTERNAL: "An internal server error occurred.",
        }
        await self._send_error(code, messages[code])
        await self.close(code=ERROR_CLOSE_CODES.get(code, CLOSE_INTERNAL_ERROR))


class AuthenticatedParticipantConsumer(ParticipantConsumer):
    """Explicit endpoint name for routing and future extension."""
