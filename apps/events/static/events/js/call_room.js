/**
 * T-30/T-31/T-34/T-40/T-41 Call Room controller — lifecycle, synchronized timer, round rotation,
 * partner presence tracking (long absence re-entry), operator live controls, and disconnect telemetry.
 *
 * Timer uses T-15 clock offset + absolute round_end_ts from server.pairing.
 * Handles T-24 messages: pairing, round_start, round_warning, round_end, event_end.
 * T-31: camera/mic are acquired once and reused across partner switches.
 * T-32: brief ICE/network drops show a degraded quality state and recover
 * on the same peer connection without ending the round or re-prompting devices.
 * T-33: 5-20s network loss reconnects the T-14 socket with bounded backoff
 * to the same partner/room/round, showing RECONNECTING while the T-15 timer
 * keeps ticking from round_end_ts.
 * T-38: a pair-scoped second media adapter may take over if the primary/direct
 * path fails. Call-room code uses only the shared T-26 transport surface.
 */

import { ClockSyncClient } from "./clock_sync_client.js";
import {
  PerfectNegotiator,
  DEFAULT_MEDIA_CONSTRAINTS,
  acquireSharedLocalMedia,
} from "./perfect_negotiator.js";

export const TimerVisualState = Object.freeze({
  WAITING: "waiting",
  NORMAL: "normal",
  WARNING: "warning",
  EXPIRED: "expired",
  PAUSED: "paused",
});

export const CallRoomPhase = Object.freeze({
  IDLE: "idle",
  PRECONNECTING: "preconnecting",
  IN_ROUND: "in_round",
  ROUND_ENDING: "round_ending",
  EVENT_ENDED: "event_ended",
  DISCONNECTED: "disconnected",
});

/** Initial wait before the first T-14 reconnect attempt (T-33). */
export const RECONNECT_INITIAL_DELAY_MS = 500;
/** Cap so restoration of a 5–20s outage still reconnects in <10s. */
export const RECONNECT_MAX_DELAY_MS = 4000;
/** Stop hammering a dead socket; identity hold on the server is 300s. */
export const RECONNECT_WINDOW_MS = 45_000;
/** Partner absence grace period before marking as gone (T-34). */
export const PARTNER_ABSENCE_GRACE_MS = 25_000;
export const PARTNER_GONE_FOOTER =
  "Your partner left. The round timer continues — no replacement will be assigned.";

export function partnerLeftBanner(name = "Partner") {
  return `${name} left. Waiting for them to return — the round continues.`;
}

/**
 * Maps protocol state/presence strings or payloads to client presence strings ('connected', 'reconnecting', 'gone').
 */
export function partnerPresenceFromProtocol(payload) {
  if (!payload) return null;
  const raw =
    typeof payload === "object" && payload !== null
      ? payload.state || payload.presence || payload.status
      : payload;
  if (!raw) return null;
  const val = String(raw).toLowerCase();
  if (val === "disconnected" || val === "gone") return "gone";
  if (val === "reconnecting") return "reconnecting";
  if (val === "connected") return "connected";
  return val;
}

/** Mirrors call_room_logic.next_reconnect_delay_ms */
export function nextReconnectDelayMs(
  attempt,
  initialMs = RECONNECT_INITIAL_DELAY_MS,
  maxMs = RECONNECT_MAX_DELAY_MS,
) {
  const n = Math.max(0, Number(attempt) || 0);
  return Math.min(maxMs, initialMs * 2 ** n);
}

/** Mirrors call_room_logic.reconnect_window_exhausted */
export function reconnectWindowExhausted(
  startedAtMs,
  nowMs,
  windowMs = RECONNECT_WINDOW_MS,
) {
  return nowMs - startedAtMs >= windowMs;
}

/** Mirrors call_room_logic.compute_remaining_ms */
export function computeRemainingMs(roundEndTs, offsetMs, clientNowMs) {
  const estimatedServerNow = Math.round(clientNowMs + offsetMs);
  return Math.max(0, roundEndTs - estimatedServerNow);
}

/** Mirrors call_room_logic.format_timer_display */
export function formatTimerDisplay(remainingMs) {
  const totalSeconds = Math.max(0, Math.floor(remainingMs / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

/** Mirrors call_room_logic.timer_visual_state */
export function timerVisualState(
  remainingMs,
  warningThresholdSeconds,
  serverWarningActive = false,
  isPaused = false,
) {
  if (isPaused) return TimerVisualState.PAUSED;
  if (remainingMs <= 0) return TimerVisualState.EXPIRED;
  if (serverWarningActive || remainingMs <= warningThresholdSeconds * 1000) {
    return TimerVisualState.WARNING;
  }
  return TimerVisualState.NORMAL;
}

/** Mirrors call_room_logic.parse_pairing_payload */
export function parsePairingPayload(payload) {
  const tags = Array.isArray(payload.partner_tags)
    ? payload.partner_tags.map(String)
    : [];
  const displayName =
    typeof payload.partner_display_name === "string" && payload.partner_display_name.trim()
      ? payload.partner_display_name
      : "Partner";

  return {
    partnerId: String(payload.partner_id),
    roomId: String(payload.room_id),
    roundNumber: Number(payload.round_number),
    roundStartTs: Number(payload.round_start_ts),
    roundEndTs: Number(payload.round_end_ts),
    isOfferer: Boolean(payload.is_offerer),
    displayName,
    tags,
  };
}

/**
 * Main call-room orchestrator.
 */
export class CallRoomController {
  constructor({
    myParticipantId,
    warningThresholdSeconds = 30,
    elements = {},
    onPhaseChange = null,
    onTimerTick = null,
    fetchIceServers = null,
    negotiatorFactory = null,
    transientIceGraceMs = null,
    iceRestartAfterDisconnectMs = null,
    reconnectInitialDelayMs = RECONNECT_INITIAL_DELAY_MS,
    reconnectMaxDelayMs = RECONNECT_MAX_DELAY_MS,
    reconnectWindowMs = RECONNECT_WINDOW_MS,
    createWebSocket = null,
    now = null,
  }) {
    this.myParticipantId = myParticipantId;
    this.warningThresholdSeconds = warningThresholdSeconds;
    this.elements = elements;
    this.onPhaseChange = onPhaseChange;
    this.onTimerTick = onTimerTick;
    this._fetchIceServers = fetchIceServers;
    this._negotiatorFactory = negotiatorFactory;
    this._transientIceGraceMs = transientIceGraceMs;
    this._iceRestartAfterDisconnectMs = iceRestartAfterDisconnectMs;
    this._reconnectInitialDelayMs = reconnectInitialDelayMs;
    this._reconnectMaxDelayMs = reconnectMaxDelayMs;
    this._reconnectWindowMs = reconnectWindowMs;
    this._createWebSocket = createWebSocket;
    this._now = now || (() => Date.now());

    this.ws = null;
    this.negotiator = null;
    this.clockSync = new ClockSyncClient();
    this.phase = CallRoomPhase.IDLE;
    this.roundEndTs = null;
    this.roundNumber = null;
    this.serverWarningActive = false;
    this.isPaused = false;
    this.partnerPresence = null;
    this.partner = null;
    this.eventEndReason = null;
    this._timerInterval = null;
    this._reconnectTimer = null;
    this._partnerGoneTimer = null;
    this._helloClientTs = null;
    this._sharedStream = null;
    this._mediaPromise = null;
    this._signalingDegraded = false;
    this._wsGeneration = 0;
    this._reconnectAttempt = 0;
    this._reconnectStartedAt = null;
    this._reconnectGaveUp = false;
    this._onlineHandler = null;
    this._beforeUnloadHandler = null;
    this._intentionalClose = false;
  }

  _liveStream(stream) {
    return Boolean(
      stream && stream.getTracks().some((track) => track.readyState !== "ended"),
    );
  }

  connect() {
    void this.ensureLocalMedia();
    this._bindOnlineListener();
    this._bindBeforeUnloadListener();
    if (this.phase === CallRoomPhase.EVENT_ENDED || this._reconnectGaveUp) return;

    const now = this._now();
    if (
      this._reconnectStartedAt != null &&
      reconnectWindowExhausted(
        this._reconnectStartedAt,
        now,
        this._reconnectWindowMs,
      )
    ) {
      this._giveUpReconnect();
      return;
    }

    const ws = this.ws;
    if (ws && ws.readyState === 1) return;

    this._openSocket();

    if (this._signalingDegraded || this.phase === CallRoomPhase.DISCONNECTED) {
      this._scheduleReconnect();
    }
  }

  _openSocket() {
    this._detachSocket();
    const generation = ++this._wsGeneration;
    const host =
      typeof location !== "undefined" && location.host
        ? location.host
        : "localhost";
    const protocol =
      typeof location !== "undefined" && location.protocol === "https:"
        ? "wss"
        : "ws";
    const url = `${protocol}://${host}/ws/events/`;
    const factory =
      this._createWebSocket ||
      (typeof WebSocket !== "undefined" ? (target) => new WebSocket(target) : null);
    if (!factory) return;

    this.ws = factory(url);
    const socket = this.ws;

    socket.onopen = () => {
      if (generation !== this._wsGeneration || this.ws !== socket) return;
      this._clearReconnect();
      this._reconnectAttempt = 0;
      this._reconnectStartedAt = null;
      this._reconnectGaveUp = false;
      this._helloClientTs = this._now();
      socket.send(
        JSON.stringify({
          type: "client.hello",
          version: 1,
          payload: { client_ts: this._helloClientTs },
        }),
      );
    };

    socket.onmessage = (event) => {
      if (generation !== this._wsGeneration || this.ws !== socket) return;
      this._handleMessage(JSON.parse(event.data));
    };

    socket.onclose = (event) => {
      if (generation !== this._wsGeneration || this.ws !== socket) return;
      if (event && (event.code === 4001 || event.reason === "session_replaced")) {
        this._onSessionReplaced();
        return;
      }
      this._onSignalingClosed();
    };

    socket.onerror = () => {
      // onclose will handle reconnect
    };
  }

  _detachSocket() {
    const socket = this.ws;
    if (!socket) return;
    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;
    try {
      if (socket.readyState === 0 || socket.readyState === 1) {
        socket.close();
      }
    } catch {
      // ignore
    }
    this.ws = null;
  }

  disconnect() {
    this._intentionalClose = true;
    this._signalingDegraded = false;
    this.isPaused = false;
    this._clearPartnerGone();
    this._unbindOnlineListener();
    this._unbindBeforeUnloadListener();
    this._clearReconnect();
    this._reconnectGaveUp = false;
    this._reconnectStartedAt = null;
    this._reconnectAttempt = 0;
    this._wsGeneration += 1;
    this._stopTimer();
    this.clockSync.stop();
    if (this.negotiator) {
      this.negotiator.leave();
      this.negotiator = null;
    }
    this._sharedStream = null;
    this._mediaPromise = null;
    this._detachSocket();
    this._intentionalClose = false;
  }

  async _handleMessage(msg) {
    const { type, payload } = msg;

    switch (type) {
      case "server.hello":
        this._onServerHello(payload);
        break;
      case "server.clock_sync":
        this.clockSync.onSampleReceived(payload);
        break;
      case "server.pairing":
        await this._onPairing(payload);
        break;
      case "server.round_start":
        await this._onRoundStart(payload);
        break;
      case "server.round_warning":
        this._onRoundWarning(payload);
        break;
      case "server.round_end":
        await this._onRoundEnd(payload);
        break;
      case "server.event_end":
        await this._onEventEnd(payload);
        break;
      case "server.error":
        this._onServerError(payload);
        break;
      case "server.session_replaced":
        this._onSessionReplaced();
        break;
      case "server.partner_state":
      case "server.partner_presence":
        this._onPartnerState(payload);
        break;
      case "operator.pause":
        this._onOperatorPause(payload);
        break;
      case "operator.resume":
        this._onOperatorResume(payload);
        break;
      case "operator.extend":
        this._onOperatorExtend(payload);
        break;
      default:
        if (type.startsWith("server.webrtc.") && this.negotiator) {
          await this.negotiator.handleSignalingMessage(msg);
        }
        break;
    }
  }

  _onServerHello(payload) {
    if (this._helloClientTs !== null && payload.server_ts) {
      this.clockSync.applyHelloOffset(this._helloClientTs, payload.server_ts);
    }
    this.clockSync.start((envelope) => {
      if (this.ws?.readyState === 1) {
        this.ws.send(JSON.stringify(envelope));
      }
    });

    const resumeRound =
      this._signalingDegraded &&
      this.phase !== CallRoomPhase.DISCONNECTED &&
      this.phase !== CallRoomPhase.IDLE &&
      this.phase !== CallRoomPhase.EVENT_ENDED;
    this._signalingDegraded = false;
    if (resumeRound) {
      this._refreshQualityFromPeer();
      void this.ensureLocalMedia();
      this.negotiator?.recoverAfterSignalingRestore?.();
      return;
    }

    this._setPhase(CallRoomPhase.IDLE);
    void this.ensureLocalMedia();
  }

  async ensureLocalMedia() {
    const existing = this._liveStream(this._sharedStream)
      ? this._sharedStream
      : this._liveStream(this.negotiator?.localStream)
        ? this.negotiator.localStream
        : null;
    if (existing) {
      this._sharedStream = existing;
      this._bindLocalPreview();
      return existing;
    }
    this._sharedStream = null;
    if (!this._mediaPromise) {
      this._mediaPromise = acquireSharedLocalMedia(DEFAULT_MEDIA_CONSTRAINTS)
        .then((stream) => {
          this._sharedStream = stream;
          this._bindLocalPreview();
          return stream;
        })
        .catch((err) => {
          this._mediaPromise = null;
          if (this.ws && this.ws.readyState === 1) {
            try {
              this.ws.send(
                JSON.stringify({
                  type: "client.telemetry",
                  payload: {
                    cause: "permission_denied",
                    detail: err.name || String(err),
                  },
                }),
              );
            } catch {
              // ignore
            }
          }
          this._showConnectionError(
            "Could not start the camera or microphone. Check permissions and try again.",
          );
          throw err;
        });
    }
    return this._mediaPromise;
  }

  async _onPairing(payload) {
    const next = parsePairingPayload(payload);
    const sameAssignment =
      Boolean(this.negotiator) &&
      Boolean(this.partner) &&
      String(this.partner.partnerId) === String(next.partnerId) &&
      String(this.partner.roomId) === String(next.roomId) &&
      Number(this.partner.roundNumber) === Number(next.roundNumber) &&
      this._isSamePartnerAssignment();

    this.partner = next;
    this.roundNumber = this.partner.roundNumber;
    this.roundEndTs = this.partner.roundEndTs;
    this.serverWarningActive = false;
    this.isPaused = false;
    this.partnerPresence = "connected";
    this._clearConnectionError();
    this._clearPartnerGone();
    this._updatePartnerUI();

    if (sameAssignment) {
      this._bindLocalPreview();
      if (!this._timerInterval) this._startTimer();
      else this._tickTimer();
      this.negotiator?.recoverAfterSignalingRestore?.();
      return;
    }

    this._setPhase(CallRoomPhase.PRECONNECTING);

    const rtcConfig = await this._loadRtcConfig();

    try {
      await this.ensureLocalMedia();

      if (!this.negotiator) {
        this.negotiator = this._createNegotiator(
          this.partner.roomId,
          this.partner.partnerId,
          rtcConfig,
        );
        await this.negotiator.acquireLocalMedia(DEFAULT_MEDIA_CONSTRAINTS);
        this._sharedStream = this.negotiator.localStream;
        this._bindLocalPreview();
        await this.negotiator.preconnect(this.partner.roomId, this.partner.partnerId);
        if (this.partner.isOfferer) {
          await this.negotiator.createManualOffer();
        }
      } else {
        this._bindLocalPreview();
        const sameAssignment = this._isSamePartnerAssignment();
        if (!sameAssignment) {
          await this.negotiator.prepareRound(this.partner.roomId, this.partner.partnerId);
          if (this.partner.isOfferer) {
            await this.negotiator.createManualOffer();
          }
        }
      }
    } catch (err) {
      this._onPartnerSwitchFailure(err);
      return;
    }

    this._startTimer();
  }

  async _onRoundStart(payload) {
    this.roundNumber = Number(payload.round_number ?? this.roundNumber);
    this._setPhase(CallRoomPhase.IN_ROUND);
    this.serverWarningActive = false;
    this.isPaused = false;

    if (this.elements.statusFooter && !this.isPaused) {
      this.elements.statusFooter.textContent = "Round in progress";
    }

    if (this.negotiator) {
      await this.negotiator.open();
    }
    this._tickTimer();
  }

  _onRoundWarning(payload) {
    this.serverWarningActive = true;
    if (payload.round_end_ts) {
      this.roundEndTs = Number(payload.round_end_ts);
    }
    if (!this.isPaused) {
      this._tickTimer();
    }
  }

  async _onRoundEnd(payload) {
    this.roundNumber = Number(payload.round_number ?? this.roundNumber);
    this._setPhase(CallRoomPhase.ROUND_ENDING);
    this.serverWarningActive = false;
    this.isPaused = false;
    this._clearPartnerGone();

    if (this.negotiator) {
      await this.negotiator.endRound();
    }
    if (this.elements.remoteVideo) {
      this.elements.remoteVideo.srcObject = null;
    }
    this._bindLocalPreview();
    this.partner = null;
    this._updatePartnerUI();
  }

  async _onEventEnd(payload) {
    this._intentionalClose = true;
    this.eventEndReason = payload.reason || "completed";
    this._setPhase(CallRoomPhase.EVENT_ENDED);
    this._signalingDegraded = false;
    this.isPaused = false;
    this._clearPartnerGone();
    this._unbindOnlineListener();
    this._unbindBeforeUnloadListener();
    this._clearReconnect();
    this._wsGeneration += 1;
    this._stopTimer();
    this.clockSync.stop();

    if (this.negotiator) {
      this.negotiator.leave();
      this.negotiator = null;
    }
    this._sharedStream = null;
    this._mediaPromise = null;
    if (this.elements.localVideo) {
      this.elements.localVideo.srcObject = null;
    }
    if (this.elements.remoteVideo) {
      this.elements.remoteVideo.srcObject = null;
    }
    this._detachSocket();
    this._intentionalClose = false;
  }

  _onServerError(payload) {
    const code = payload?.code ? String(payload.code) : "";
    const reason = payload?.reason ? String(payload.reason) : "";
    const msg = payload?.message || payload?.error || "";
    if (
      code === "ERR_ALREADY_CONNECTED" ||
      code === "session_replaced" ||
      reason === "session_replaced" ||
      code === "4001" ||
      /another window/i.test(msg)
    ) {
      this._onSessionReplaced();
      return;
    }
    this._showConnectionError(msg || "A connection error occurred.");
  }

  _onSessionReplaced() {
    this._intentionalClose = true;
    this._clearPartnerGone();
    this._clearReconnect();
    this._unbindOnlineListener();
    this._unbindBeforeUnloadListener();
    this._stopTimer();
    this.clockSync.stop();

    if (this.negotiator) {
      this.negotiator.leave();
      this.negotiator = null;
    }
    this._sharedStream = null;
    this._mediaPromise = null;
    if (this.elements.localVideo) {
      this.elements.localVideo.srcObject = null;
    }
    if (this.elements.remoteVideo) {
      this.elements.remoteVideo.srcObject = null;
    }
    this._detachSocket();
    this._intentionalClose = false;

    if (this.elements.errorBanner) {
      this.elements.errorBanner.hidden = false;
      this.elements.errorBanner.textContent =
        "You joined from another window. This session is no longer active.";
    }
    if (this.elements.statusFooter) {
      this.elements.statusFooter.textContent =
        "You joined from another window. This session is no longer active.";
    }
    if (this.elements.connectionBadge) {
      this.elements.connectionBadge.textContent = "SESSION REPLACED";
      this.elements.connectionBadge.className = "badge failed";
      if (!this.elements.connectionBadge.dataset) {
        this.elements.connectionBadge.dataset = {};
      }
      this.elements.connectionBadge.dataset.quality = "session-replaced";
    }
  }

  _showPartnerGone() {
    const name = this.partner?.displayName || "Partner";
    const bannerText = partnerLeftBanner(name);
    if (this.elements.partnerGoneBanner) {
      this.elements.partnerGoneBanner.hidden = false;
      this.elements.partnerGoneBanner.textContent = bannerText;
    }
    if (this.elements.qualityBanner) {
      this.elements.qualityBanner.hidden = false;
      this.elements.qualityBanner.textContent = bannerText;
    }
    if (this.elements.remoteVideo) {
      this.elements.remoteVideo.srcObject = null;
    }
    if (this.elements.connectionBadge) {
      this.elements.connectionBadge.textContent = "PARTNER LEFT";
      this.elements.connectionBadge.className = "badge partner-gone";
      if (!this.elements.connectionBadge.dataset) {
        this.elements.connectionBadge.dataset = {};
      }
      this.elements.connectionBadge.dataset.quality = "partner-gone";
    }
    if (this.elements.statusFooter) {
      this.elements.statusFooter.textContent = PARTNER_GONE_FOOTER;
    }
    this._updatePartnerUI();
  }

  _onPartnerState(payload) {
    if (!payload) return;
    const roomId = payload.room_id || payload.roomId;
    if (roomId && this.partner && String(roomId) !== String(this.partner.roomId)) {
      return;
    }
    const partnerId = payload.partner_id || payload.partnerId;
    if (partnerId && this.partner && String(partnerId) !== String(this.partner.partnerId)) {
      return;
    }

    const presence = partnerPresenceFromProtocol(payload) || "gone";
    this.partnerPresence = presence;

    if (presence === "gone") {
      this._showPartnerGone();
    } else if (presence === "reconnecting") {
      if (this.elements.qualityBanner) {
        this.elements.qualityBanner.hidden = false;
        this.elements.qualityBanner.textContent =
          "Partner temporarily disconnected. Please wait...";
      }
      if (this.elements.connectionBadge) {
        this.elements.connectionBadge.textContent = "PARTNER RECONNECTING";
        this.elements.connectionBadge.className = "badge reconnecting";
        if (!this.elements.connectionBadge.dataset) {
          this.elements.connectionBadge.dataset = {};
        }
        this.elements.connectionBadge.dataset.quality = "partner-reconnecting";
      }
    } else if (presence === "connected") {
      this._clearPartnerGone();
    }
  }

  _onPartnerPresence(payload) {
    this._onPartnerState(payload);
  }

  _clearPartnerGone() {
    if (this._partnerGoneTimer) {
      clearTimeout(this._partnerGoneTimer);
      this._partnerGoneTimer = null;
    }
    if (this.elements.partnerGoneBanner) {
      this.elements.partnerGoneBanner.hidden = true;
      this.elements.partnerGoneBanner.textContent = "";
    }
    if (this.elements.qualityBanner && !this._signalingDegraded && !this.isPaused) {
      this.elements.qualityBanner.hidden = true;
      this.elements.qualityBanner.textContent = "";
    }
    if (this.elements.statusFooter) {
      this.elements.statusFooter.textContent = "Round in progress";
    }
    if (
      this.elements.connectionBadge &&
      this.elements.connectionBadge.dataset?.quality === "partner-gone"
    ) {
      this._applyConnectionQuality(
        this.negotiator?.pc?.connectionState || "connected",
      );
    }
  }

  _onOperatorPause(payload) {
    this.isPaused = true;
    this._stopTimer();

    const remainingSeconds = Number(payload.remaining_seconds ?? 0);
    const remainingMs = remainingSeconds * 1000;
    const display = formatTimerDisplay(remainingMs);
    this._renderTimer(display, TimerVisualState.PAUSED);

    if (this.elements.qualityBanner) {
      this.elements.qualityBanner.hidden = false;
      this.elements.qualityBanner.textContent =
        "Round temporarily paused by operator.";
    }

    if (this.onTimerTick) {
      this.onTimerTick({
        remainingMs,
        display,
        visual: TimerVisualState.PAUSED,
      });
    }
  }

  _onOperatorResume(payload) {
    this.isPaused = false;
    if (payload.ends_at) {
      this.roundEndTs =
        typeof payload.ends_at === "number"
          ? payload.ends_at
          : Date.parse(payload.ends_at);
    }

    if (this.elements.qualityBanner && !this._signalingDegraded) {
      this.elements.qualityBanner.hidden = true;
      this.elements.qualityBanner.textContent = "";
    }

    this._startTimer();
  }

  _onOperatorExtend(payload) {
    if (payload.ends_at) {
      this.roundEndTs =
        typeof payload.ends_at === "number"
          ? payload.ends_at
          : Date.parse(payload.ends_at);
    } else if (payload.extended_by_seconds && this.roundEndTs) {
      this.roundEndTs += Number(payload.extended_by_seconds) * 1000;
    }

    if (!this.isPaused) {
      this._tickTimer();
    }
  }

  _createNegotiator(roomId, partnerId, rtcConfig) {
    const options = {
      myParticipantId: this.myParticipantId,
      partnerParticipantId: partnerId,
      roomId,
      rtcConfig,
      localStream: this._sharedStream || null,
      sendSignalingMessage: (signalMsg) => {
        if (this.ws?.readyState === 1) {
          this.ws.send(JSON.stringify(signalMsg));
        }
      },
      onRemoteStream: (stream) => {
        if (this.elements.remoteVideo && this.elements.remoteVideo.srcObject !== stream) {
          this.elements.remoteVideo.srcObject = stream;
          this.elements.remoteVideo.play().catch(() => {});
        }
      },
      onStateChange: (state) => {
        if (state === "closed" && this.phase !== CallRoomPhase.EVENT_ENDED) {
          return;
        }
        this._applyConnectionQuality(state);
      },
      onFailure: (reason) => {
        if (this._signalingDegraded && !this._reconnectGaveUp) return;
        this._onPartnerSwitchFailure(new Error(reason), reason);
      },
    };
    if (this._transientIceGraceMs != null) {
      options.transientIceGraceMs = this._transientIceGraceMs;
    }
    if (this._iceRestartAfterDisconnectMs != null) {
      options.iceRestartAfterDisconnectMs = this._iceRestartAfterDisconnectMs;
    }
    options.deferPermanentFailure = () =>
      this._signalingDegraded && !this._reconnectGaveUp;
    if (this._negotiatorFactory) {
      return this._negotiatorFactory(options);
    }
    return new PerfectNegotiator(options);
  }

  _isSamePartnerAssignment() {
    if (!this.negotiator || !this.partner) return false;
    const pc = this.negotiator.pc;
    const pcUsable =
      pc && pc.signalingState !== "closed" && pc.connectionState !== "closed";
    return (
      pcUsable &&
      String(this.negotiator.roomId) === String(this.partner.roomId) &&
      String(this.negotiator.partnerId) === String(this.partner.partnerId)
    );
  }

  _bindLocalPreview() {
    const stream = this.negotiator?.localStream || this._sharedStream;
    if (this.elements.localVideo && stream) {
      if (this.elements.localVideo.srcObject !== stream) {
        this.elements.localVideo.srcObject = stream;
      }
    }
  }

  _applyConnectionQuality(state) {
    if (this._signalingDegraded && state !== "connected") {
      this._showReconnecting();
      return;
    }
    if (state === "connected") {
      this._clearConnectionError();
    }
    if (this.elements.connectionBadge) {
      const labels = {
        connecting: "CONNECTING",
        connected: "CONNECTED",
        degraded: "QUALITY DROP",
        failed: "FAILED",
        closed: "CLOSED",
      };
      this.elements.connectionBadge.textContent =
        labels[state] || String(state).toUpperCase();
      this.elements.connectionBadge.className = `badge ${state}`;
      if (!this.elements.connectionBadge.dataset) {
        this.elements.connectionBadge.dataset = {};
      }
      this.elements.connectionBadge.dataset.quality = state;
    }
    if (this.elements.qualityBanner) {
      const degraded = state === "degraded";
      this.elements.qualityBanner.hidden = !degraded;
      if (degraded) {
        this.elements.qualityBanner.textContent =
          "Temporary quality drop — recovering connection…";
      }
    }
  }

  _refreshQualityFromPeer() {
    const conn = this.negotiator?.pc?.connectionState;
    if (conn === "connected") {
      this._applyConnectionQuality("connected");
    } else if (conn === "disconnected" || conn === "failed") {
      this._applyConnectionQuality("degraded");
    }
  }

  _onSignalingClosed() {
    if (this._intentionalClose) return;
    if (this.phase === CallRoomPhase.EVENT_ENDED) return;

    this._detachSocket();

    const keepRound =
      this.phase === CallRoomPhase.IN_ROUND ||
      this.phase === CallRoomPhase.PRECONNECTING ||
      this.phase === CallRoomPhase.ROUND_ENDING;

    this.clockSync.stop();

    if (keepRound) {
      this._signalingDegraded = true;
      this._showReconnecting();
      this._scheduleReconnect();
      return;
    }

    this._setPhase(CallRoomPhase.DISCONNECTED);
    this._stopTimer();
    this._showReconnecting();
    this._scheduleReconnect();
  }

  _onPartnerSwitchFailure(err, reason = "PARTNER_SWITCH_FAILED") {
    console.error("[CallRoom] Partner switch/renegotiation failed:", err);
    const message =
      reason === "ICE_CONNECTION_FAILED"
        ? "Connection to this partner failed. Your camera stays on; other rounds are not stopped."
        : "Could not connect to the next partner. Your camera stays on and other rounds are not stopped.";
    this._showConnectionError(message);
    if (this.elements.connectionBadge) {
      this.elements.connectionBadge.textContent = `FAILED: ${reason}`;
      this.elements.connectionBadge.className = "badge failed";
    }
    this._bindLocalPreview();
  }

  _showConnectionError(message) {
    if (this.elements.errorBanner) {
      this.elements.errorBanner.hidden = false;
      this.elements.errorBanner.textContent = message;
    }
  }

  _clearConnectionError() {
    if (this.elements.errorBanner) {
      this.elements.errorBanner.hidden = true;
      this.elements.errorBanner.textContent = "";
    }
  }

  async _loadRtcConfig() {
    if (!this._fetchIceServers) return {};
    try {
      const ice = await this._fetchIceServers();
      return { iceServers: ice.iceServers || [] };
    } catch {
      return {};
    }
  }

  _startTimer() {
    this._stopTimer();
    this._timerInterval = setInterval(() => this._tickTimer(), 250);
    this._tickTimer();
  }

  _stopTimer() {
    if (this._timerInterval) {
      clearInterval(this._timerInterval);
      this._timerInterval = null;
    }
  }

  _tickTimer() {
    if (this.isPaused) return;
    if (!this.roundEndTs || !this.clockSync.isSynchronised) {
      this._renderTimer("--:--", TimerVisualState.WAITING);
      return;
    }

    const remainingMs = computeRemainingMs(
      this.roundEndTs,
      this.clockSync.offsetMs,
      Date.now(),
    );
    const visual = timerVisualState(
      remainingMs,
      this.warningThresholdSeconds,
      this.serverWarningActive,
      this.isPaused,
    );
    const display = formatTimerDisplay(remainingMs);
    this._renderTimer(display, visual);

    if (this.onTimerTick) {
      this.onTimerTick({ remainingMs, display, visual });
    }
  }

  _renderTimer(display, visual) {
    if (this.elements.timerDisplay) {
      this.elements.timerDisplay.textContent = display;
      this.elements.timerDisplay.dataset.state = visual;
    }
    if (this.elements.timerContainer) {
      this.elements.timerContainer.dataset.state = visual;
    }
  }

  _updatePartnerUI() {
    if (this.elements.partnerName) {
      this.elements.partnerName.textContent = this.partner?.displayName ?? "—";
    }
    if (this.elements.partnerTags) {
      const tags = this.partner?.tags ?? [];
      this.elements.partnerTags.textContent = tags.length ? tags.join(", ") : "";
      this.elements.partnerTags.hidden = !tags.length;
    }
    if (this.elements.roundLabel) {
      this.elements.roundLabel.textContent =
        this.roundNumber != null ? `Round ${this.roundNumber}` : "—";
    }
    if (this.elements.roomLabel) {
      this.elements.roomLabel.textContent = this.partner?.roomId ?? "—";
    }
  }

  _setPhase(phase) {
    this.phase = phase;
    if (this.elements.phaseBadge) {
      this.elements.phaseBadge.textContent = phase.replace(/_/g, " ").toUpperCase();
      this.elements.phaseBadge.dataset.phase = phase;
    }
    if (this.onPhaseChange) {
      this.onPhaseChange(phase);
    }
  }

  _scheduleReconnect() {
    if (this.phase === CallRoomPhase.EVENT_ENDED) return;
    this._clearReconnect();
    const now = this._now();
    if (this._reconnectStartedAt == null) {
      this._reconnectStartedAt = now;
    }
    if (
      reconnectWindowExhausted(
        this._reconnectStartedAt,
        now,
        this._reconnectWindowMs,
      )
    ) {
      this._giveUpReconnect();
      return;
    }
    const delay = nextReconnectDelayMs(
      this._reconnectAttempt,
      this._reconnectInitialDelayMs,
      this._reconnectMaxDelayMs,
    );
    this._reconnectAttempt += 1;
    this._reconnectTimer = setTimeout(() => {
      this._reconnectTimer = null;
      if (
        reconnectWindowExhausted(
          this._reconnectStartedAt,
          this._now(),
          this._reconnectWindowMs,
        )
      ) {
        this._giveUpReconnect();
        return;
      }
      this.connect();
    }, delay);
  }

  _giveUpReconnect() {
    this._reconnectGaveUp = true;
    this._clearReconnect();
    this._detachSocket();
    this._showConnectionError(
      "Could not restore the connection. The round was not ended for other participants.",
    );
    if (this.elements.connectionBadge) {
      this.elements.connectionBadge.textContent = "FAILED";
      this.elements.connectionBadge.className = "badge failed";
      if (!this.elements.connectionBadge.dataset) {
        this.elements.connectionBadge.dataset = {};
      }
      this.elements.connectionBadge.dataset.quality = "failed";
    }
  }

  _showReconnecting() {
    if (this.elements.connectionBadge) {
      this.elements.connectionBadge.textContent = "RECONNECTING";
      this.elements.connectionBadge.className = "badge reconnecting";
      if (!this.elements.connectionBadge.dataset) {
        this.elements.connectionBadge.dataset = {};
      }
      this.elements.connectionBadge.dataset.quality = "reconnecting";
    }
    if (this.elements.qualityBanner) {
      this.elements.qualityBanner.hidden = false;
      this.elements.qualityBanner.textContent =
        "RECONNECTING — restoring the same round and partner…";
    }
    if (this.elements.statusFooter) {
      this.elements.statusFooter.textContent =
        "RECONNECTING — round timer continues";
    }
  }

  _bindOnlineListener() {
    if (this._onlineHandler) return;
    const target = typeof window !== "undefined" ? window : globalThis;
    if (typeof target.addEventListener !== "function") return;
    this._onlineHandler = () => {
      if (this.phase === CallRoomPhase.EVENT_ENDED) return;
      if (!this._signalingDegraded && this.phase !== CallRoomPhase.DISCONNECTED) {
        return;
      }
      this._reconnectGaveUp = false;
      this._reconnectAttempt = 0;
      this._reconnectStartedAt = this._now();
      this._clearReconnect();
      this.connect();
    };
    target.addEventListener("online", this._onlineHandler);
  }

  _unbindOnlineListener() {
    if (!this._onlineHandler) return;
    const target = typeof window !== "undefined" ? window : globalThis;
    if (typeof target.removeEventListener === "function") {
      target.removeEventListener("online", this._onlineHandler);
    }
    this._onlineHandler = null;
  }

  _bindBeforeUnloadListener() {
    if (this._beforeUnloadHandler) return;
    const target = typeof window !== "undefined" ? window : globalThis;
    if (typeof target.addEventListener !== "function") return;
    this._beforeUnloadHandler = () => {
      if (this.ws && this.ws.readyState === 1) {
        try {
          this.ws.send(
            JSON.stringify({
              type: "client.telemetry",
              payload: { cause: "tab_closed" },
            }),
          );
        } catch {
          // ignore
        }
      }
    };
    target.addEventListener("beforeunload", this._beforeUnloadHandler);
  }

  _unbindBeforeUnloadListener() {
    if (!this._beforeUnloadHandler) return;
    const target = typeof window !== "undefined" ? window : globalThis;
    if (typeof target.removeEventListener === "function") {
      target.removeEventListener("beforeunload", this._beforeUnloadHandler);
    }
    this._beforeUnloadHandler = null;
  }

  _clearReconnect() {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }
}