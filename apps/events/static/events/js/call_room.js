/**
 * T-30/T-31 Call Room controller — lifecycle, synchronized timer, round rotation.
 *
 * Timer uses T-15 clock offset + absolute round_end_ts from server.pairing.
 * Handles T-24 messages: pairing, round_start, round_warning, round_end, event_end.
 * T-31: camera/mic are acquired once and reused across partner switches.
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
});

export const CallRoomPhase = Object.freeze({
  IDLE: "idle",
  PRECONNECTING: "preconnecting",
  IN_ROUND: "in_round",
  ROUND_ENDING: "round_ending",
  EVENT_ENDED: "event_ended",
  DISCONNECTED: "disconnected",
});

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
export function timerVisualState(remainingMs, warningThresholdSeconds, serverWarningActive = false) {
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
  }) {
    this.myParticipantId = myParticipantId;
    this.warningThresholdSeconds = warningThresholdSeconds;
    this.elements = elements;
    this.onPhaseChange = onPhaseChange;
    this.onTimerTick = onTimerTick;
    this._fetchIceServers = fetchIceServers;
    this._negotiatorFactory = negotiatorFactory;

    this.ws = null;
    this.negotiator = null;
    this.clockSync = new ClockSyncClient();
    this.phase = CallRoomPhase.IDLE;
    this.roundEndTs = null;
    this.roundNumber = null;
    this.serverWarningActive = false;
    this.partner = null;
    this.eventEndReason = null;
    this._timerInterval = null;
    this._reconnectTimer = null;
    this._helloClientTs = null;
    this._sharedStream = null;
    this._mediaPromise = null;
  }

  _liveStream(stream) {
    return Boolean(
      stream && stream.getTracks().some((track) => track.readyState !== "ended"),
    );
  }

  connect() {
    void this.ensureLocalMedia();
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    this.ws = new WebSocket(`${protocol}://${location.host}/ws/events/`);

    this.ws.onopen = () => {
      this._clearReconnect();
      this._helloClientTs = Date.now();
      this.ws.send(
        JSON.stringify({
          type: "client.hello",
          version: 1,
          payload: { client_ts: this._helloClientTs },
        }),
      );
    };

    this.ws.onmessage = (event) => {
      this._handleMessage(JSON.parse(event.data));
    };

    this.ws.onclose = () => {
      this._setPhase(CallRoomPhase.DISCONNECTED);
      this._stopTimer();
      this.clockSync.stop();
      this._scheduleReconnect();
    };

    this.ws.onerror = () => {
      // onclose will handle reconnect
    };
  }

  disconnect() {
    this._clearReconnect();
    this._stopTimer();
    this.clockSync.stop();
    if (this.negotiator) {
      this.negotiator.leave();
      this.negotiator = null;
    }
    this._sharedStream = null;
    this._mediaPromise = null;
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
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
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify(envelope));
      }
    });
    this._setPhase(CallRoomPhase.IDLE);
    void this.ensureLocalMedia();
  }

  /**
   * Acquire camera/mic once for the whole event. Pairing and reconnect
   * reuse this stream and never call getUserMedia again.
   */
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
          this._showConnectionError(
            "Could not start the camera or microphone. Check permissions and try again.",
          );
          throw err;
        });
    }
    return this._mediaPromise;
  }

  async _onPairing(payload) {
    this.partner = parsePairingPayload(payload);
    this.roundNumber = this.partner.roundNumber;
    this.roundEndTs = this.partner.roundEndTs;
    this.serverWarningActive = false;
    this._clearConnectionError();
    this._updatePartnerUI();
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
    this._tickTimer();
  }

  async _onRoundEnd(payload) {
    this.roundNumber = Number(payload.round_number ?? this.roundNumber);
    this._setPhase(CallRoomPhase.ROUND_ENDING);
    this.serverWarningActive = false;

    if (this.negotiator) {
      await this.negotiator.endRound();
    }
    if (this.elements.remoteVideo) {
      this.elements.remoteVideo.srcObject = null;
    }
    this._bindLocalPreview();
    this.partner = null;
    this._updatePartnerUI();
    // Next server.pairing will prepare the next round automatically.
  }

  async _onEventEnd(payload) {
    this.eventEndReason = payload.reason || "completed";
    this._setPhase(CallRoomPhase.EVENT_ENDED);
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
    if (this.ws) {
      this.ws.close();
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
        if (this.ws?.readyState === WebSocket.OPEN) {
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
        if (this.elements.connectionBadge) {
          this.elements.connectionBadge.textContent = state.toUpperCase();
          this.elements.connectionBadge.className = `badge ${state}`;
        }
      },
      onFailure: (reason) => {
        this._onPartnerSwitchFailure(new Error(reason), reason);
      },
    };
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
    // Do not leave(), stop tracks, close the WebSocket, or end the event.
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
    this._reconnectTimer = setTimeout(() => this.connect(), 2000);
  }

  _clearReconnect() {
    if (this._reconnectTimer) {
      clearTimeout(this._reconnectTimer);
      this._reconnectTimer = null;
    }
  }
}
