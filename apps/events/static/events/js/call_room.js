/**
/**
 * T-30/T-31/T-34/T-40/T-41 Call Room controller — lifecycle, synchronized timer, round rotation,
 * partner presence tracking (long absence re-entry), operator live controls, and disconnect telemetry.
 *
 * Timer uses T-15 clock offset + absolute round_end_ts from server.pairing.
 * Handles T-24 messages: pairing, round_start, round_warning, round_end, event_end.
 * T-31: camera/mic are acquired once and reused across partner switches.
 * T-32: brief ICE/network drops show a degraded quality state and recover
 * on the same peer connection without ending the round or re-prompting devices.
 * T-33: 5–20s network loss reconnects the T-14 socket with bounded backoff
 * to the same partner/room/round, showing RECONNECTING while the T-15 timer
 * keeps ticking from round_end_ts.
 * T-38: a pair-scoped second media adapter may take over if the primary/direct
 * path fails. Call-room code uses only the shared T-26 transport surface.
 * T-39: if the primary path is not connected in time, the same T-38 fallback
 * is activated for THIS pair only.
 * T-40/T-41: partner presence tracking and operator live controls.
 */

import { ClockSyncClient } from "./clock_sync_client.js";
import {
  FailoverMediaTransport,
  DEFAULT_MEDIA_CONSTRAINTS,
  DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS,
  acquireSharedLocalMedia,
} from "./media_transport.js";

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

export const RECONNECT_INITIAL_DELAY_MS = 500;
export const RECONNECT_MAX_DELAY_MS = 4000;
export const RECONNECT_WINDOW_MS = 60_000;
export const PARTNER_ABSENCE_GRACE_MS = 60_000;
export const PARTNER_GONE_FOOTER =
  "Please stay here while we set up your next date.";

export function partnerLeftBanner(name = "Partner") {
  return `${name}'s connection dropped. We'll hold this date for 1 minute. If ${name} comes back in time, you'll continue together. If not, we'll match you with your next date.`;
}

export function partnerPresenceFromProtocol(
  payload,
) {
  if (!payload) {
    return null;
  }

  const raw =
    typeof payload === "object" &&
    payload !== null
      ? payload.state ||
        payload.presence ||
        payload.status
      : payload;

  if (!raw) {
    return null;
  }

  const val =
    String(raw).toLowerCase();

  if (
    val === "disconnected" ||
    val === "gone"
  ) {
    return "gone";
  }

  if (
    val === "reconnecting"
  ) {
    return "reconnecting";
  }

  if (
    val === "connected"
  ) {
    return "connected";
  }

  return val;
}

export function nextReconnectDelayMs(
  attempt,
  initialMs =
    RECONNECT_INITIAL_DELAY_MS,
  maxMs =
    RECONNECT_MAX_DELAY_MS,
) {
  const n = Math.max(
    0,
    Number(attempt) || 0,
  );

  return Math.min(
    maxMs,
    initialMs * 2 ** n,
  );
}

export function reconnectWindowExhausted(
  startedAtMs,
  nowMs,
  windowMs =
    RECONNECT_WINDOW_MS,
) {
  return (
    nowMs - startedAtMs >=
    windowMs
  );
}

export function computeRemainingMs(
  roundEndTs,
  offsetMs,
  clientNowMs,
) {
  const estimatedServerNow =
    Math.round(
      clientNowMs + offsetMs,
    );

  return Math.max(
    0,
    roundEndTs - estimatedServerNow,
  );
}

export function formatTimerDisplay(
  remainingMs,
) {
  const totalSeconds =
    Math.max(
      0,
      Math.floor(
        remainingMs / 1000,
      ),
    );

  const minutes =
    Math.floor(
      totalSeconds / 60,
    );

  const seconds =
    totalSeconds % 60;

  return `${String(
    minutes,
  ).padStart(
    2,
    "0",
  )}:${String(seconds).padStart(
    2,
    "0",
  )}`;
}

export function timerVisualState(
  remainingMs,
  warningThresholdSeconds,
  serverWarningActive = false,
  isPaused = false,
) {
  if (isPaused) {
    return TimerVisualState.PAUSED;
  }

  if (remainingMs <= 0) {
    return TimerVisualState.EXPIRED;
  }

  if (
    serverWarningActive ||
    remainingMs <=
      warningThresholdSeconds *
        1000
  ) {
    return TimerVisualState.WARNING;
  }

  return TimerVisualState.NORMAL;
}

export function parsePairingPayload(
  payload,
) {
  const tags =
    Array.isArray(
      payload.partner_tags,
    )
      ? payload.partner_tags.map(
          String,
        )
      : [];

  const displayName =
    typeof payload.partner_display_name ===
      "string" &&
    payload.partner_display_name.trim()
      ? payload.partner_display_name
      : "Partner";

  return {
    partnerId: String(
      payload.partner_id,
    ),
    roomId: String(
      payload.room_id,
    ),
    roundNumber: Number(
      payload.round_number,
    ),
    roundStartTs: Number(
      payload.round_start_ts,
    ),
    roundEndTs: Number(
      payload.round_end_ts,
    ),
    isOfferer: Boolean(
      payload.is_offerer,
    ),
    displayName,
    tags,
  };
}

export class CallRoomController {
  constructor({
    myParticipantId,
    warningThresholdSeconds = 30,

    forceMediaFallback = false,
    forceFallback = false,

    mediaFallbackRoomId = "",

    escalationTimeoutMs =
      DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS,

    scheduleEscalation = null,

    elements = {},

    onPhaseChange = null,
    onTimerTick = null,

    fetchIceServers = null,
    submitInterest = null,

    negotiatorFactory = null,
    transportFactory = null,

    transientIceGraceMs = null,
    iceRestartAfterDisconnectMs = null,

    reconnectInitialDelayMs =
      RECONNECT_INITIAL_DELAY_MS,

    reconnectMaxDelayMs =
      RECONNECT_MAX_DELAY_MS,

    reconnectWindowMs =
      RECONNECT_WINDOW_MS,

    earlyExitMinMinutes = 3,

    createWebSocket = null,
    now = null,
  }) {
    this.myParticipantId =
      myParticipantId;

    this.warningThresholdSeconds =
      warningThresholdSeconds;

    /*
     * Pair-scoped fallback configuration.
     *
     * IMPORTANT:
     * Fallback is only allowed when the current room matches
     * mediaFallbackRoomId.
     */
    this.mediaFallbackRoomId =
      String(
        mediaFallbackRoomId || "",
      );

    /*
     * Keep the underscore aliases used by the main branch.
     * This allows both branches' code paths to use the same values.
     */
    this._mediaFallbackRoomId =
      this.mediaFallbackRoomId;

    this.forceMediaFallbackEnabled =
      Boolean(
        forceMediaFallback ||
          forceFallback,
      );

    this._forceMediaFallback =
      this.forceMediaFallbackEnabled;

    this._escalationTimeoutMs =
      Number.isFinite(
        Number(
          escalationTimeoutMs,
        ),
      )
        ? Number(
            escalationTimeoutMs,
          )
        : DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS;

    this._scheduleEscalation =
      scheduleEscalation;

    this.elements = elements;

    this.onPhaseChange =
      onPhaseChange;

    this.onTimerTick =
      onTimerTick;

    this._fetchIceServers =
      fetchIceServers;

    this._submitInterest =
      submitInterest;

    this._interestPrompts =
      new Map();

    this._activeInterestPromptId =
      null;

    this._interestTimer =
      null;

    this._interestSubmitting =
      false;

    /*
     * transportFactory has priority over negotiatorFactory
     * because the newer test/transport abstraction uses it.
     *
     * The older negotiatorFactory remains supported for compatibility.
     */
    this._negotiatorFactory =
      transportFactory ||
      negotiatorFactory;

    this._transientIceGraceMs =
      transientIceGraceMs;

    this._iceRestartAfterDisconnectMs =
      iceRestartAfterDisconnectMs;

    this._reconnectInitialDelayMs =
      reconnectInitialDelayMs;

    this._reconnectMaxDelayMs =
      reconnectMaxDelayMs;

    this._reconnectWindowMs =
      reconnectWindowMs;

    this._createWebSocket =
      createWebSocket;

    this._now =
      now || (() => Date.now());

    this.ws = null;
    this.negotiator = null;

    this.clockSync =
      new ClockSyncClient();

    this.phase =
      CallRoomPhase.IDLE;

    this.roundEndTs = null;
    this.roundNumber = null;

    this.serverWarningActive =
      false;

    this.isPaused = false;

    this.partnerPresence =
      null;

    this.partner = null;

    this.eventEndReason =
      null;

    this._timerInterval =
      null;

    this._reconnectTimer =
      null;

    this._partnerGoneTimer =
      null;

    this._permanentIceFailureTimer =
      null;

    /*
     * Generation token for permanent ICE failure notifications.
     *
     * Prevents stale timers from affecting:
     * - a new partner,
     * - a replacement transport,
     * - a completed round,
     * - an ended event.
     */
    this._permanentIceFailureGeneration =
      0;

    this._helloClientTs =
      null;

    this._sharedStream =
      null;

    this._observedMediaStreams = new WeakSet();

    this._mediaPromise =
      null;

    this._signalingDegraded =
      false;

    this._wsGeneration =
      0;
    this._messageQueue = Promise.resolve();
    this._signalQueues = new Map();
    this._sessionInactive = false;

    this._reconnectAttempt =
      0;

    this._reconnectStartedAt =
      null;

    this._reconnectGaveUp =
      false;

    this._onlineHandler =
      null;

    this._beforeUnloadHandler =
      null;

    this._intentionalClose =
      false;

    this.earlyExitMinMinutes =
      Number(earlyExitMinMinutes) || 3;
    this._leaveDateTimer = null;
    this._autoReloadTimer = null;
    this._standbyTimer = null;

    if (this.elements.leaveDateBtn) {
      this.elements.leaveDateBtn.addEventListener('click', () => {
        if (this.elements.leaveDateConfirmModal) {
          this.elements.leaveDateConfirmModal.hidden = false;
        } else {
          this.leaveDateEarly();
        }
      });
    }
    if (this.elements.cancelLeaveDateBtn) {
      this.elements.cancelLeaveDateBtn.addEventListener('click', () => {
        if (this.elements.leaveDateConfirmModal) {
          this.elements.leaveDateConfirmModal.hidden = true;
        }
      });
    }
    if (this.elements.confirmLeaveDateBtn) {
      this.elements.confirmLeaveDateBtn.addEventListener('click', () => {
        this.leaveDateEarly();
      });
    }
    if (this.elements.cameraButton) {
      this.elements.cameraButton.addEventListener("click", () => {
        void this.toggleCamera();
      });
    }
  }

  async toggleCamera() {
    const button = this.elements.cameraButton;
    const track = this._sharedStream?.getVideoTracks?.()[0];
    if (this._mediaBlocked || !track) {
      this._mediaBlocked = false;
      this._mediaPromise = null;
      const stream = await this.ensureLocalMedia();
      if (!stream) return;
      this._sendClientReady();
      if (button) {
        button.textContent = "Camera on";
        button.setAttribute("aria-pressed", "true");
      }
      this._setGuide("Camera and microphone are on. We'll connect you when your date is ready.");
      return;
    }
    track.enabled = !track.enabled;
    const card = button?.closest?.(".video-card");
    if (card) card.classList.toggle("camera-off", !track.enabled);
    if (button) {
      button.textContent = track.enabled ? "Camera on" : "Camera off";
      button.setAttribute("aria-pressed", String(track.enabled));
    }
    this._setGuide(
      track.enabled
        ? "You're on your date."
        : "Your camera is off. Tap it again when you want to be seen.",
    );
  }

  get transport() {
    return this.negotiator;
  }

  set transport(val) {
    this.negotiator = val;
  }

  _liveStream(stream) {
    return Boolean(
      stream &&
        stream.getAudioTracks?.().some((track) => track.readyState !== "ended") &&
        stream.getVideoTracks?.().some((track) => track.readyState !== "ended"),
    );
  }

  async forceMediaFallback() {
    if (!this.negotiator) {
      return false;
    }

    if (
      typeof this.negotiator
        .forceMediaFallback ===
      "function"
    ) {
      await this.negotiator.forceMediaFallback();
    } else if (
      typeof this.negotiator
        .forceFallback ===
      "function"
    ) {
      await this.negotiator.forceFallback();
    } else {
      return false;
    }

    this._bindLocalPreview();

    this._applyConnectionQuality(
      "relay",
    );

    return true;
  }

  async forceFallback() {
    return this.forceMediaFallback();
  }

  connect() {
    if (this._sessionInactive || this.phase === CallRoomPhase.EVENT_ENDED) return;
    void this.ensureLocalMedia();

    this._bindOnlineListener();
    this._bindBeforeUnloadListener();

    if (
      this.phase ===
        CallRoomPhase.EVENT_ENDED ||
      this._reconnectGaveUp
    ) {
      return;
    }

    const now =
      this._now();

    if (
      this._reconnectStartedAt !=
        null &&
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

    if (
      ws &&
      ws.readyState === 1
    ) {
      return;
    }

    this._openSocket();

    if (
      this._signalingDegraded ||
      this.phase ===
        CallRoomPhase.DISCONNECTED
    ) {
      this._scheduleReconnect();
    }
  }

  _openSocket() {
    this._detachSocket();

    const generation =
      ++this._wsGeneration;

    const host =
      typeof location !==
        "undefined" &&
      location.host
        ? location.host
        : "localhost";

    const protocol =
      typeof location !==
        "undefined" &&
      location.protocol ===
        "https:"
        ? "wss"
        : "ws";

    const url =
      `${protocol}://${host}/ws/events/`;

    const factory =
      this._createWebSocket ||
      (typeof WebSocket !==
      "undefined"
        ? (target) =>
            new WebSocket(
              target,
            )
        : null);

    if (!factory) {
      return;
    }

    this.ws =
      factory(url);
    this._serverHelloReceived = false;
    this._readySentGeneration = null;

    const socket =
      this.ws;

    socket.onopen = () => {
      if (
        generation !==
          this._wsGeneration ||
        this.ws !== socket
      ) {
        return;
      }

      this._clearReconnect();
      this._clearEarlyExitTimer();
      this._clearStandbyCountdown();
      this._clearAutoReload();

      if (this.elements.disconnectNotificationBanner) {
        this.elements.disconnectNotificationBanner.hidden = true;
      }

      this._reconnectAttempt =
        0;

      this._reconnectStartedAt =
        null;

      this._reconnectGaveUp =
        false;

      this._helloClientTs =
        this._now();

      socket.send(
        JSON.stringify({
          type: "client.hello",
          version: 1,
          payload: {
            client_ts:
              this._helloClientTs,
          },
        }),
      );
    };

    socket.onmessage =
      (event) => {
        if (
          generation !==
            this._wsGeneration ||
          this.ws !== socket
        ) {
          return;
        }

        // Pairing awaits ICE configuration and device acquisition. Keep later
        // round-start and signaling messages behind it so open() cannot run
        // before preconnect() has created (and muted) the senders.
        this._messageQueue = this._messageQueue.then(async () => {
          if (generation !== this._wsGeneration || this.ws !== socket) return;
          const message = JSON.parse(event.data);
          if (message.type?.startsWith("server.webrtc.")) {
            const roomId = message.payload?.room_id;
            const previous = this._signalQueues.get(roomId) || Promise.resolve();
            const pending = previous.then(async () => {
              if (generation !== this._wsGeneration || this.ws !== socket || this.partner?.roomId !== roomId) return;
              await this._handleMessage(message);
            }).catch(() => {});
            this._signalQueues.set(roomId, pending);
            // Browser SDP promises can stay pending after an old peer closes.
            // They must not block lifecycle messages or another assigned room.
            for (const key of this._signalQueues.keys()) {
              if (key !== this.partner?.roomId) this._signalQueues.delete(key);
            }
          } else {
            await this._handleMessage(message);
          }
        }).catch(() => {
          // A malformed frame must not poison the queue or stop heartbeats.
          if (generation === this._wsGeneration && this.ws === socket) {
            this._showConnectionError("A call update could not be applied. Reconnecting…");
          }
        });
      };

    socket.onclose =
      (event) => {
        if (
          generation !==
            this._wsGeneration ||
          this.ws !== socket
        ) {
          return;
        }

        if (
          event &&
          (
            event.reason ===
              "session_replaced"
          )
        ) {
          this._onSessionReplaced();
          return;
        }

        if (event?.code === 4001) {
          this._onSessionReplaced(true);
          return;
        }

        this._onSignalingClosed();
      };

    socket.onerror = () => {};
  }

  _detachSocket() {
    const socket =
      this.ws;

    if (!socket) {
      return;
    }

    socket.onopen = null;
    socket.onmessage = null;
    socket.onclose = null;
    socket.onerror = null;

    try {
      if (
        socket.readyState ===
          0 ||
        socket.readyState ===
          1
      ) {
        socket.close();
      }
    } catch {}

    this.ws = null;
  }

  disconnect() {
    this._clearServerWaiting();
    this._intentionalClose =
      true;

    this._signalingDegraded =
      false;

    this.isPaused =
      false;

    try {
      sessionStorage.removeItem("meetakk_active_date");
    } catch (e) {}

    this._clearPartnerGone();

    this._clearPermanentIceFailureTimer();

    this._unbindOnlineListener();

    this._unbindBeforeUnloadListener();

    this._clearReconnect();
    this._clearEarlyExitTimer();
    this._clearStandbyCountdown();
    this._clearAutoReload();

    this._reconnectGaveUp =
      false;

    this._reconnectStartedAt =
      null;

    this._reconnectAttempt =
      0;

    this._wsGeneration += 1;

    this._stopTimer();

    this.clockSync.stop();

    if (this.negotiator) {
      this.negotiator.leave();
      this.negotiator = null;
    }

    this._sharedStream =
      null;

    this._mediaPromise =
      null;

    this._detachSocket();

    this._intentionalClose =
      false;
  }

  async _handleMessage(msg) {
    const { type, payload } =
      msg;

    // Queued room updates must not revive an event that has already ended.
    // A final interest prompt may still arrive after the closing message.
    if (this._sessionInactive || (this.phase === CallRoomPhase.EVENT_ENDED && type !== "server.interest_prompt")) return;

    switch (type) {
      case "server.hello":
        this._onServerHello(
          payload,
        );
        break;

      case "server.clock_sync":
        this.clockSync.onSampleReceived(
          payload,
        );
        break;

      case "server.pairing":
        this._clearServerWaiting();
        await this._onPairing(
          payload,
        );
        break;

      case "server.waiting":
        this._onServerWaiting(payload || {});
        break;

      case "server.round_start":
        await this._onRoundStart(
          payload,
        );
        break;

      case "server.round_warning":
        this._onRoundWarning(
          payload,
        );
        break;

      case "server.round_end":
        await this._onRoundEnd(
          payload,
        );
        break;

      case "server.interest_prompt":
        this._onInterestPrompt(
          payload,
        );
        break;

      case "server.event_end":
        await this._onEventEnd(
          payload,
        );
        break;

      case "server.error":
        this._onServerError(
          payload,
        );
        break;

      case "server.session_replaced":
        this._onSessionReplaced();
        break;

      case "server.pair_chat":
        this._onPairChat(payload);
        break;

      case "server.partner_state":
      case "server.partner_presence":
        this._onPartnerState(
          payload,
        );
        break;

      case "operator.pause":
        this._onOperatorPause(
          payload,
        );
        break;

      case "operator.resume":
        this._onOperatorResume(
          payload,
        );
        break;

      case "operator.extend":
        this._onOperatorExtend(
          payload,
        );
        break;

      default:
        if (
          type.startsWith(
            "server.webrtc.",
          ) &&
          this.negotiator
        ) {
          await this.negotiator.handleSignalingMessage(
            msg,
          );
        }

        break;
    }
  }

  _onServerHello(payload) {
    this._serverHelloReceived = true;
    if (
      this._helloClientTs !==
        null &&
      payload.server_ts
    ) {
      this.clockSync.applyHelloOffset(
        this._helloClientTs,
        payload.server_ts,
      );
    }

    this.clockSync.start(
      (envelope) => {
        if (
          this.ws?.readyState ===
          1
        ) {
          this.ws.send(
            JSON.stringify(
              envelope,
            ),
          );
        }
      },
    );

    const resumeRound =
      this._signalingDegraded &&
      this.phase !==
        CallRoomPhase.DISCONNECTED &&
      this.phase !==
        CallRoomPhase.IDLE &&
      this.phase !==
        CallRoomPhase.EVENT_ENDED;

    this._signalingDegraded =
      false;

    if (resumeRound) {
      this._refreshQualityFromPeer();

      void this.ensureLocalMedia().then(() => this._sendClientReady());

      this.negotiator
        ?.recoverAfterSignalingRestore?.();

      return;
    }

    this._setPhase(
      CallRoomPhase.IDLE,
    );

    try {
      const savedDate = sessionStorage.getItem('meetakk_active_date');
      if (savedDate) {
        const parsed = JSON.parse(savedDate);
        sessionStorage.removeItem('meetakk_active_date');
        const durationSec = (this._now() - (parsed.start_ts || 0)) / 1000;
        if (durationSec >= 180 && parsed.partner_name && parsed.room_id) {
          const promptId = `${parsed.round_number}:${parsed.room_id}`;
          if (!this._interestPrompts.has(promptId)) {
            const prompt = {
              round_number: parsed.round_number,
              room_id: parsed.room_id,
              partner_display_name: parsed.partner_name,
              server_ts: this._now(),
              deadline_ts: this._now() + 60000,
              own_response: null,
              mutual: false,
            };
            this._interestPrompts.set(promptId, prompt);
            this._renderInterestPrompt(promptId, prompt);
            if (this.elements.interestTitle) {
              this.elements.interestTitle.textContent = `You were disconnected from your previous date with ${parsed.partner_name}. Would you like to stay in touch with them?`;
            }
          }
        }
      }
    } catch (e) {}

    void this.ensureLocalMedia().then(() => this._sendClientReady());
  }

  _sendClientReady(ready = true) {
    if (this.ws?.readyState !== 1 || !this._serverHelloReceived) return;
    const hasUsableMedia = this._liveStream(this._sharedStream);
    if (ready && !hasUsableMedia) return;
    const readyValue = Boolean(ready && hasUsableMedia);
    if (this._readySentGeneration === this._wsGeneration && this._readySentValue === readyValue) return;
    const roundNumber = Math.max(0, Math.min(8, Number(this.roundNumber) || 0));
    try {
      this.ws.send(JSON.stringify({
        type: "client.ready",
        version: 1,
        payload: { round_number: roundNumber, ready: readyValue },
      }));
      this._readySentGeneration = this._wsGeneration;
      this._readySentValue = readyValue;
    } catch {}
  }

  _clearServerWaiting() {
    if (this.elements.waitingBanner) {
      this.elements.waitingBanner.hidden = true;
      this.elements.waitingBanner.textContent = "";
    }
  }

  _setGuide(text) {
    if (this.elements.statusFooter && text) {
      this.elements.statusFooter.textContent = text;
    }
  }

  _onServerWaiting(payload) {
    if (this._sessionInactive || this.phase === CallRoomPhase.EVENT_ENDED) return;
    const reason = String(payload.reason || "waiting_for_partner");

    if (reason === "standby_fewer_women" || reason === "standby_fewer_men") {
      if (this.elements.mainRoomStandbyBanner) {
        if (this.elements.standbyTitle) {
          this.elements.standbyTitle.textContent = "You are in the Main Room for this round";
        }
        if (this.elements.standbyMessage) {
          if (reason === "standby_fewer_women") {
            this.elements.standbyMessage.innerHTML = "We apologize, but because there are fewer women than men active in this round, you are in the Main Room for this round until your next date starts in 7 minutes.";
          } else {
            this.elements.standbyMessage.innerHTML = "We apologize, but because there are fewer men than women active in this round, you are in the Main Room for this round until your next date starts in 7 minutes.";
          }
        }
        this.elements.mainRoomStandbyBanner.hidden = false;
        this._startStandbyCountdown(payload.remaining_seconds || 420);
      }
      this._stopTimer();
      this.roundEndTs = null;
      this._renderTimer("--:--", TimerVisualState.WAITING);
      this._setPhase(CallRoomPhase.IDLE);
      return;
    } else {
      this._clearStandbyCountdown();
    }

    if (reason === "partner_holding") {
      const message = payload?.message || "We'll hold this date for 1 minute.";
      const named = String(message).match(/^(.*?)'s connection dropped/);
      const name = named ? named[1] : (this.partner?.displayName || "your partner");
      if (this.elements.waitingBanner) {
        this.elements.waitingBanner.textContent = message;
        this.elements.waitingBanner.hidden = false;
      }
      if (this.elements.qualityBanner) {
        this.elements.qualityBanner.hidden = false;
        this.elements.qualityBanner.textContent = message;
      }
      this._setGuide(`Please wait while ${name} reconnects.`);
      return;
    }

    const message = reason === "limit_reached"
      ? (payload?.message || "You have completed your dates for tonight. You will not receive any more dates. We hope you have a wonderful night!")
      : (payload?.message || "Waiting for your next partner. You’ll be connected automatically when someone is available.");
    const endsCall = reason === "partner_left" || reason === "limit_reached" || reason === "waiting_next_round" || reason === "looking_for_date";
    if (endsCall && this.negotiator && this.partner) {
      void this.negotiator.endRound?.();
      this.partner = null;
      this._updatePartnerUI();
      this._stopTimer();
      this.roundEndTs = null;
      this._renderTimer("--:--", TimerVisualState.WAITING);
      this._setPhase(CallRoomPhase.IDLE);
    }
    this._clearConnectionError();
    if (!this.elements.waitingBanner) return;
    // Server checks continue silently; repeated checks do not rewrite the live region.
    if (this.elements.waitingBanner.textContent !== message) {
      this.elements.waitingBanner.textContent = message;
    }
    if (this.elements.waitingBanner.hidden) this.elements.waitingBanner.hidden = false;
    this._setGuide(message);
  }

  async ensureLocalMedia() {
    const existing =
      this._liveStream(
        this._sharedStream,
      )
        ? this._sharedStream
        : this._liveStream(
              this.negotiator
                ?.localStream,
            )
          ? this.negotiator
              .localStream
          : null;

    if (existing) {
      this._sharedStream =
        existing;

      this._bindLocalPreview();

      return existing;
    }

    this._sharedStream =
      null;

    if (!this._mediaPromise) {
      this._mediaPromise =
        acquireSharedLocalMedia(
          DEFAULT_MEDIA_CONSTRAINTS,
        )
          .then(
            (stream) => {
              this._sharedStream =
                stream;

              this._watchLocalMediaTracks(stream);

              this._bindLocalPreview();

              this._sendClientReady();

              return stream;
            },
          )
          .catch(
            (err) => {
              this._mediaPromise =
                null;

              if (
                this.ws &&
                this.ws.readyState ===
                  1
              ) {
                try {
                  this.ws.send(
                    JSON.stringify({
                      type:
                        "client.telemetry",
                      payload: {
                        cause:
                          "permission_denied",
                        detail:
                          err?.name ||
                          String(err),
                      },
                    }),
                  );
                } catch {}
              }

              const denied =
                err?.name === "NotAllowedError" ||
                err?.name === "PermissionDeniedError";
              if (denied) {
                this._mediaBlocked = true;
                this._setGuide("Tap the camera and microphone button to join your date.");
                if (this.elements.cameraButton) {
                  this.elements.cameraButton.hidden = false;
                  this.elements.cameraButton.textContent = "Turn camera and mic on";
                }
                return null;
              }
              this._showConnectionError(
                "Could not start the camera or microphone. Check permissions and try again.",
              );

              throw err;
            },
          );
    }

    return this._mediaPromise;
  }

  _watchLocalMediaTracks(stream) {
    if (!stream || this._observedMediaStreams.has(stream)) return;
    this._observedMediaStreams.add(stream);
    for (const track of stream.getTracks()) {
      track.addEventListener?.("ended", () => {
        if (stream !== this._sharedStream) return;
        this._readySentGeneration = null;
        this._mediaPromise = null;
        this._sendClientReady(false);
        this._showConnectionError("Your camera or microphone stopped. Reconnect it, then try again.");
      }, { once: true });
    }
  }

  async _onPairing(payload) {
    const next =
      parsePairingPayload(
        payload,
      );

    const sameAssignment =
      Boolean(this.negotiator) &&
      Boolean(this.partner) &&
      String(
        this.partner.partnerId,
      ) ===
        String(
          next.partnerId,
        ) &&
      String(
        this.partner.roomId,
      ) ===
        String(
          next.roomId,
        ) &&
      Number(
        this.partner.roundNumber,
      ) ===
        Number(
          next.roundNumber,
        ) &&
      this._isSamePartnerAssignment();

    this.partner =
      next;
    if (!sameAssignment) this._dateClockEnded = false;

    this.roundNumber =
      this.partner.roundNumber;

    this.roundEndTs =
      this.partner.roundEndTs;

    this.serverWarningActive =
      false;

    this.partnerPresence =
      "connected";

    this.roundStartTs = this._now();
    try {
      sessionStorage.setItem(
        "meetakk_active_date",
        JSON.stringify({
          room_id: this.partner.roomId,
          round_number: this.partner.roundNumber,
          partner_name: this.partner.displayName,
          start_ts: this.roundStartTs,
        }),
      );
    } catch (e) {}
    if (this._breakTimer) {
      clearInterval(this._breakTimer);
      this._breakTimer = null;
    }
    if (this.elements.breakBanner) {
      this.elements.breakBanner.hidden = true;
    }

    this._clearConnectionError();
    this._clearPartnerGone();

    this._updatePartnerUI();

    if (sameAssignment) {
      this._bindLocalPreview();

      if (!this._timerInterval) {
        this._startTimer();
      } else {
        this._tickTimer();
      }

      this.negotiator
        ?.recoverAfterSignalingRestore?.();

      return;
    }

    this._setPhase(
      CallRoomPhase.PRECONNECTING,
    );

    const rtcConfig =
      await this._loadRtcConfig();

    this.negotiator?.setRtcConfig?.(rtcConfig);

    try {
      const stream = await this.ensureLocalMedia();
      if (!stream) {
        this._setGuide("Tap the camera and microphone button to join your date.");
        return;
      }

      if (!this.negotiator) {
        this.negotiator =
          this._createNegotiator(
            this.partner.roomId,
            this.partner.partnerId,
            rtcConfig,
          );

        await this.negotiator.acquireLocalMedia(
          DEFAULT_MEDIA_CONSTRAINTS,
        );

        this._sharedStream =
          this.negotiator.localStream;

        this._bindLocalPreview();

        await this.negotiator.preconnect(
          this.partner.roomId,
          this.partner.partnerId,
        );

        if (
          this.partner.isOfferer
        ) {
          await this.negotiator.createManualOffer();
        }
      } else {
        this._bindLocalPreview();

        const sameAssignment =
          this._isSamePartnerAssignment();

        if (!sameAssignment) {
          await this.negotiator.prepareRound(
            this.partner.roomId,
            this.partner.partnerId,
          );

          if (
            this.partner.isOfferer
          ) {
            await this.negotiator.createManualOffer();
          }
        }
      }
    } catch (err) {
      this._onPartnerSwitchFailure(
        err,
      );

      return;
    }

    this._startTimer();
  }

  async _onRoundStart(
    payload,
  ) {
    this.roundNumber =
      Number(
        payload.round_number ??
          this.roundNumber,
      );

    this._dateClockEnded = false;
    this._setPhase(
      CallRoomPhase.IN_ROUND,
    );

    this._setPairChatVisible(true);

    this.serverWarningActive =
      false;

    if (
      this.elements
        .statusFooter &&
      !this.isPaused
    ) {
      this.elements.statusFooter.textContent =
        "You're on your date.";
    }

    if (this.negotiator) {
      await this.negotiator.open();
    }

    this._startEarlyExitTimer();
    this._tickTimer();
  }

  _onRoundWarning(payload) {
    const nextEnd = Number(payload?.round_end_ts || 0);
    if (this.roundEndTs && nextEnd && nextEnd + 1500 < this.roundEndTs) {
      return;
    }
    this.serverWarningActive =
      true;

    if (payload.round_end_ts) {
      this.roundEndTs =
        Number(
          payload.round_end_ts,
        );
    }

    if (!this.isPaused) {
      this._tickTimer();
    }
  }

  async _onRoundEnd(payload) {
    const serverNow = this._now() + (this.clockSync?.offsetMs || 0);
    if (this.roundEndTs && serverNow < this.roundEndTs - 1500) {
      return;
    }
    this._clearEarlyExitTimer();
    this._setPairChatVisible(false);
    this.roundNumber =
      Number(
        payload.round_number ??
          this.roundNumber,
      );

    this._setPhase(
      CallRoomPhase.ROUND_ENDING,
    );

    this.serverWarningActive =
      false;

    this.isPaused =
      false;

    try {
      sessionStorage.removeItem("meetakk_active_date");
    } catch (e) {}

    this._clearPartnerGone();
    this._clearPermanentIceFailureTimer();

    if (this.negotiator) {
      await this.negotiator.endRound();
    }

    if (
      this.elements.remoteVideo
    ) {
      this.elements.remoteVideo.srcObject =
        null;
    }

    this._bindLocalPreview();

    this.partner = null;

    this._updatePartnerUI();

    const breakDurationSec = Number(payload.break_duration_seconds ?? 120);
    this._startBreakCountdown(breakDurationSec);
    this._clearAutoReload();
    this._showNextInterestPrompt();
  }

  _startBreakCountdown(durationSec) {
    if (this._breakTimer) {
      clearInterval(this._breakTimer);
      this._breakTimer = null;
    }
    if (this.elements.breakBanner) {
      this.elements.breakBanner.hidden = false;
    }
    if (this.elements.roundFeedbackPanel) {
      this.elements.roundFeedbackPanel.hidden = false;
    }
    let remaining = durationSec;
    const updateDisplay = () => {
      if (this.elements.breakCountdown) {
        const m = Math.floor(remaining / 60);
        const s = remaining % 60;
        this.elements.breakCountdown.textContent = `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
      }
    };
    updateDisplay();
    this._breakTimer = setInterval(() => {
      remaining = Math.max(0, remaining - 1);
      updateDisplay();
      if (remaining <= 0) {
        clearInterval(this._breakTimer);
        this._breakTimer = null;
      }
    }, 1000);
  }

  _setPairChatVisible(visible) {
    if (this.elements.pairChatPanel) {
      this.elements.pairChatPanel.hidden = !visible;
    }
    if (!visible && this.elements.pairChatLog) {
      this.elements.pairChatLog.textContent = "";
      this.elements.pairChatPanel?.classList.remove("has-messages");
    }
  }

  _appendChatMessage(senderLabel, text) {
    if (!this.elements.pairChatLog || !text) {
      return;
    }
    const line = document.createElement("div");
    line.className = "pair-chat-line";
    line.textContent = `${senderLabel}: ${text}`;
    this.elements.pairChatLog.appendChild(line);
    this.elements.pairChatLog.scrollTop = this.elements.pairChatLog.scrollHeight;
    this.elements.pairChatPanel?.classList.add("has-messages");
  }

  _onPairChat(payload) {
    const fromId = String(payload?.from_participant_id || "");
    if (fromId && fromId === String(this.myParticipantId)) {
      return;
    }
    const text = String(payload?.text || "").trim();
    if (text) {
      this._appendChatMessage("Partner", text);
    }
  }

  sendPairChat(text) {
    const roomId = this.partner?.roomId;
    if (
      !roomId ||
      this.phase !== CallRoomPhase.IN_ROUND ||
      !this.ws ||
      this.ws.readyState !== WebSocket.OPEN
    ) {
      return false;
    }
    const body = String(text || "").trim();
    if (!body) {
      return false;
    }
    try {
      this.ws.send(
        JSON.stringify({
          type: "client.pair_chat",
          version: 1,
          payload: { room_id: roomId, text: body.slice(0, 500) },
        }),
      );
      this._appendChatMessage("You", body);
      return true;
    } catch {
      return false;
    }
  }

  _onInterestPrompt(payload) {
    const roomId = String(payload.room_id || "");
    const roundNumber = Number(payload.round_number);
    const deadlineTs = Number(payload.deadline_ts);
    if (!roomId || !Number.isFinite(roundNumber) || !Number.isFinite(deadlineTs)) {
      return;
    }

    const promptId = `${roundNumber}:${roomId}`;
    const previous = this._interestPrompts.get(promptId) || {};
    const prompt = {
      ...previous,
      ...payload,
      room_id: roomId,
      round_number: roundNumber,
      deadline_ts: deadlineTs,
      partner_display_name: String(payload.partner_display_name || "your partner"),
      own_response:
        typeof payload.own_response === "boolean"
          ? payload.own_response
          : null,
      mutual: Boolean(payload.mutual),
      partner_whatsapp:
        typeof payload.partner_whatsapp === "string"
          ? payload.partner_whatsapp
          : null,
    };
    this._interestPrompts.set(promptId, prompt);
    if (this._activeInterestPromptId === promptId) {
      this._renderInterestPrompt(promptId, prompt);
    } else {
      this._showNextInterestPrompt();
    }
  }

  _showNextInterestPrompt() {
    if (this._activeInterestPromptId !== null) {
      return;
    }

    const now = this._now() + (this.clockSync.offsetMs || 0);
    for (const [promptId, prompt] of this._interestPrompts) {
      if (!prompt.mutual && prompt.deadline_ts < now) {
        this._interestPrompts.delete(promptId);
      }
    }
    const next = [...this._interestPrompts.entries()]
      .filter(([, prompt]) => prompt.mutual || prompt.deadline_ts >= now)
      .sort((a, b) => {
        const aHasOpenWindow = !a[1].mutual && a[1].deadline_ts >= now;
        const bHasOpenWindow = !b[1].mutual && b[1].deadline_ts >= now;
        if (aHasOpenWindow !== bHasOpenWindow) return aHasOpenWindow ? -1 : 1;
        return a[1].deadline_ts - b[1].deadline_ts;
      })[0];
    if (!next) {
      return;
    }

    const [promptId, prompt] = next;
    this._renderInterestPrompt(promptId, prompt);
  }

  _renderInterestPrompt(promptId, prompt) {
    if (this._activeInterestPromptId !== promptId) {
      this._interestSubmitting = false;
    }
    this._activeInterestPromptId = promptId;
    if (this.elements.interestDialog) {
      this.elements.interestDialog.hidden = false;
    }
    if (this.elements.waitingBanner) {
      this.elements.waitingBanner.hidden = true;
    }
    if (this.elements.qualityBanner) {
      this.elements.qualityBanner.hidden = true;
    }
    if (this.elements.interestPartnerName) {
      this.elements.interestPartnerName.textContent =
        prompt.partner_display_name;
    }

    const hasResponded = prompt.own_response !== null;
    if (this.elements.interestChoiceButtons) {
      this.elements.interestChoiceButtons.hidden = hasResponded || prompt.mutual;
    }
    if (this.elements.interestCloseButton) {
      this.elements.interestCloseButton.hidden = !hasResponded && !prompt.mutual;
    }
    if (this.elements.interestCountdownWrap) {
      this.elements.interestCountdownWrap.hidden = prompt.mutual;
    }
    if (this.elements.interestStatus) {
      this.elements.interestStatus.textContent = prompt.mutual
        ? "You both said yes. You can now contact each other on WhatsApp."
        : hasResponded
          ? "Your choice is saved. If you both say yes, your WhatsApp numbers will appear here."
          : "If you both say yes, your WhatsApp numbers will be shared here.";
    }
    if (this.elements.interestContact) {
      this.elements.interestContact.hidden = !prompt.mutual;
    }
    if (this.elements.interestContactLink) {
      const phone = String(prompt.partner_whatsapp || "").trim();
      const digits = phone.replace(/\D/g, "");
      this.elements.interestContactLink.textContent = phone;
      this.elements.interestContactLink.hidden = !digits;
      if (digits) {
        this.elements.interestContactLink.href = `https://wa.me/${digits}`;
      } else {
        this.elements.interestContactLink.removeAttribute("href");
      }
    }
    if (this.elements.interestContactMissing) {
      this.elements.interestContactMissing.hidden = Boolean(
        String(prompt.partner_whatsapp || "").replace(/\D/g, ""),
      );
    }

    if (prompt.mutual) {
      this._stopInterestCountdown();
    } else {
      this._startInterestCountdown();
      this._tickInterestCountdown();
    }
  }

  _startInterestCountdown() {
    this._stopInterestCountdown();
    this._interestTimer = setInterval(
      () => this._tickInterestCountdown(),
      250,
    );
    if (this._interestTimer?.unref) {
      this._interestTimer.unref();
    }
  }

  _stopInterestCountdown() {
    if (this._interestTimer) {
      clearInterval(this._interestTimer);
      this._interestTimer = null;
    }
  }

  _tickInterestCountdown() {
    if (this._activeInterestPromptId === null) {
      this._stopInterestCountdown();
      return;
    }
    const prompt = this._interestPrompts.get(this._activeInterestPromptId);
    if (!prompt) {
      this._finishInterestPrompt();
      return;
    }
    if (prompt.mutual) {
      this._stopInterestCountdown();
      return;
    }

    const remainingMs = Math.max(
      0,
      prompt.deadline_ts - (this._now() + (this.clockSync.offsetMs || 0)),
    );
    if (this.elements.interestCountdown) {
      this.elements.interestCountdown.textContent = String(
        Math.ceil(remainingMs / 1000),
      );
    }
    if (remainingMs === 0) {
      if (this._interestSubmitting) {
        if (this.elements.interestStatus) {
          this.elements.interestStatus.textContent = "Saving your choice…";
        }
        return;
      }
      if (this.elements.interestStatus) {
        this.elements.interestStatus.textContent =
          "This choice window has ended.";
      }
      if (this.elements.interestChoiceButtons) {
        this.elements.interestChoiceButtons.hidden = true;
      }
      this._finishInterestPrompt();
    }
  }

  async submitInterestChoice(interested) {
    const prompt = this._interestPrompts.get(this._activeInterestPromptId);
    if (
      !prompt ||
      prompt.own_response !== null ||
      this._interestSubmitting ||
      typeof this._submitInterest !== "function"
    ) {
      return;
    }
    this._interestSubmitting = true;
    this._setInterestButtonsDisabled(true);
    if (this.elements.interestStatus) {
      this.elements.interestStatus.textContent = "Saving your choice…";
    }

    try {
      const result = await this._submitInterest({
        room_id: prompt.room_id,
        round_number: prompt.round_number,
        interested: Boolean(interested),
      });
      if (!result || result.ok === false) {
        const errCode = result?.error?.code;
        const errMsg =
          result?.error?.message || "Your choice could not be saved.";
        if (errCode === "choice_not_open") {
          if (this.elements.interestStatus) {
            this.elements.interestStatus.textContent =
              "The choice window is opening — please try again in a moment.";
          }
          this._setInterestButtonsDisabled(false);
          return;
        }
        if (errCode === "choice_already_recorded") {
          prompt.own_response = Boolean(interested);
          this._renderInterestPrompt(this._activeInterestPromptId, prompt);
          return;
        }
        throw new Error(errMsg);
      }
      prompt.own_response = Boolean(interested);
      prompt.mutual = Boolean(result.mutual);
      prompt.partner_whatsapp = result.partner_whatsapp || null;
      this._renderInterestPrompt(this._activeInterestPromptId, prompt);
    } catch (error) {
      if (this.elements.interestStatus) {
        this.elements.interestStatus.textContent =
          error.message || "Your choice could not be saved. Please try again.";
      }
      this._setInterestButtonsDisabled(false);
    } finally {
      this._interestSubmitting = false;
    }
  }

  _setInterestButtonsDisabled(disabled) {
    for (const button of [
      this.elements.interestYesButton,
      this.elements.interestNoButton,
    ]) {
      if (button) {
        button.disabled = disabled;
      }
    }
  }

  dismissInterestPrompt() {
    this._finishInterestPrompt();
  }

  _finishInterestPrompt() {
    const promptId = this._activeInterestPromptId;
    this._stopInterestCountdown();
    if (promptId !== null) {
      this._interestPrompts.delete(promptId);
    }
    this._activeInterestPromptId = null;
    this._interestSubmitting = false;
    if (this.elements.interestDialog) {
      this.elements.interestDialog.hidden = true;
    }
    this._showNextInterestPrompt();
    if (this._activeInterestPromptId === null && this.phase === CallRoomPhase.IDLE) {
      this._onServerWaiting({ reason: "waiting_for_partner" });
    }
  }

  async _onEventEnd(payload) {
    this._clearServerWaiting();
    this._intentionalClose =
      true;

    this.eventEndReason =
      payload.reason ||
      "completed";

    this._setPhase(
      CallRoomPhase.EVENT_ENDED,
    );

    this._signalingDegraded =
      false;

    this.isPaused =
      false;

    try {
      sessionStorage.removeItem("meetakk_active_date");
    } catch (e) {}

    this._clearPartnerGone();
    this._clearPermanentIceFailureTimer();

    this._unbindOnlineListener();
    this._unbindBeforeUnloadListener();

    this._clearReconnect();
    this._clearEarlyExitTimer();
    this._clearStandbyCountdown();
    this._clearAutoReload();

    this._wsGeneration += 1;

    this._stopTimer();

    this.roundEndTs = null;
    this.serverWarningActive = false;
    this._renderTimer("--:--", TimerVisualState.WAITING);

    this.clockSync.stop();

    if (this.negotiator) {
      this.negotiator.leave();
      this.negotiator = null;
    }

    this._sharedStream =
      null;

    this._mediaPromise =
      null;

    if (
      this.elements.localVideo
    ) {
      this.elements.localVideo.srcObject =
        null;
    }

    if (
      this.elements.remoteVideo
    ) {
      this.elements.remoteVideo.srcObject =
        null;
    }

    this._detachSocket();

    this._applyConnectionQuality("closed");
    this._clearConnectionError();
    if (this.elements.statusFooter) {
      this.elements.statusFooter.textContent = "The event has ended. Thank you for being here.";
    }

    this._intentionalClose =
      false;
  }

  _onServerError(payload) {
    const code =
      payload?.code
        ? String(payload.code)
        : "";

    const reason =
      payload?.reason
        ? String(payload.reason)
        : "";

    const msg =
      payload?.message ||
      payload?.error ||
      "";

    if (code === "ERR_NOT_AUTHENTICATED" || code === "4001") {
      this._onSessionReplaced(true);
      return;
    }

    if (
      code ===
        "ERR_ALREADY_CONNECTED" ||
      code ===
        "session_replaced" ||
      reason ===
        "session_replaced" ||
      /another window/i.test(
        msg,
      )
    ) {
      this._onSessionReplaced();

      return;
    }

    this._showConnectionError(
      msg ||
        "A connection error occurred.",
    );
  }

  _onSessionReplaced(authenticationRequired = false) {
    if (this._sessionInactive) return;
    this._sessionInactive = true;
    this._wsGeneration += 1;
    this._clearServerWaiting();
    const message = authenticationRequired
      ? "Your access session is no longer valid. Reopen your personal event link from your email."
      : "Your personal link is active in another tab or device. Close the other room tabs, then reopen your personal event link here.";
    this._intentionalClose =
      true;

    this._clearPartnerGone();
    this._clearPermanentIceFailureTimer();
    this._clearReconnect();
    this._clearEarlyExitTimer();
    this._clearStandbyCountdown();
    this._clearAutoReload();

    this._unbindOnlineListener();
    this._unbindBeforeUnloadListener();

    this._stopTimer();
    this.roundEndTs = null;
    this._renderTimer("--:--", TimerVisualState.WAITING);
    this.clockSync.stop();

    if (this.negotiator) {
      this.negotiator.leave();
      this.negotiator = null;
    }

    this._sharedStream =
      null;

    this._mediaPromise =
      null;

    if (
      this.elements.localVideo
    ) {
      this.elements.localVideo.srcObject =
        null;
    }

    if (
      this.elements.remoteVideo
    ) {
      this.elements.remoteVideo.srcObject =
        null;
    }

    this._detachSocket();

    this._intentionalClose =
      false;

    if (
      this.elements.errorBanner
    ) {
      this.elements.errorBanner.hidden =
        false;

      this.elements.errorBanner.textContent =
        message;
    }

    if (
      this.elements.statusFooter
    ) {
      this.elements.statusFooter.textContent =
        message;
    }

    if (
      this.elements.connectionBadge
    ) {
      this.elements.connectionBadge.textContent =
        authenticationRequired ? "ACCESS LINK REQUIRED" : "SESSION REPLACED";

      this.elements.connectionBadge.className =
        "badge failed";

      if (
        !this.elements
          .connectionBadge.dataset
      ) {
        this.elements.connectionBadge.dataset =
          {};
      }

      this.elements.connectionBadge.dataset.quality =
        authenticationRequired ? "authentication-required" : "session-replaced";
    }
  }

  _showPartnerGone() {
    const name =
      this.partner?.displayName ||
      "Partner";

    const bannerText =
      partnerLeftBanner(
        name,
      );

    if (
      this.elements
        .partnerGoneBanner
    ) {
      this.elements.partnerGoneBanner.hidden =
        false;

      this.elements.partnerGoneBanner.textContent =
        bannerText;
    }

    if (
      this.elements.qualityBanner
    ) {
      this.elements.qualityBanner.hidden =
        false;

      this.elements.qualityBanner.textContent =
        bannerText;
    }

    if (
      this.elements.remoteVideo
    ) {
      this.elements.remoteVideo.srcObject =
        null;
    }

    if (
      this.elements.connectionBadge
    ) {
      this.elements.connectionBadge.textContent =
        "PARTNER LEFT";

      this.elements.connectionBadge.className =
        "badge partner-gone";

      if (
        !this.elements
          .connectionBadge.dataset
      ) {
        this.elements.connectionBadge.dataset =
          {};
      }

      this.elements.connectionBadge.dataset.quality =
        "partner-gone";
    }

    if (
      this.elements.statusFooter
    ) {
      this.elements.statusFooter.textContent =
        PARTNER_GONE_FOOTER;
    }

    this._updatePartnerUI();

    if (this._partnerDisconnectWaitTimer) {
      clearTimeout(this._partnerDisconnectWaitTimer);
      this._partnerDisconnectWaitTimer = null;
    }

    const currentPartner = this.partner;
    const conversationDurationSec = this.roundStartTs ? (this._now() - this.roundStartTs) / 1000 : 0;

    this._partnerDisconnectWaitTimer = setTimeout(() => {
      if (this.partnerPresence === "gone" && currentPartner) {
        if (conversationDurationSec >= 180) {
          const promptId = `${currentPartner.roundNumber || this.roundNumber}:${currentPartner.roomId}`;
          const prompt = {
            round_number: currentPartner.roundNumber || this.roundNumber,
            room_id: currentPartner.roomId,
            partner_display_name: name,
            server_ts: this._now(),
            deadline_ts: this._now() + 60000,
            own_response: null,
            mutual: false,
          };
          this._interestPrompts.set(promptId, prompt);
          this._renderInterestPrompt(promptId, prompt);
          if (this.elements.interestTitle) {
            this.elements.interestTitle.textContent = `Your date with ${name} ended early. Did you like them?`;
          }
        }
      }
    }, 60000);
  }

  _onPartnerState(payload) {
    if (!payload) {
      return;
    }

    const roomId =
      payload.room_id ||
      payload.roomId;

    if (
      roomId &&
      this.partner &&
      String(roomId) !==
        String(
          this.partner.roomId,
        )
    ) {
      return;
    }

    const partnerId =
      payload.partner_id ||
      payload.partnerId;

    if (
      partnerId &&
      this.partner &&
      String(partnerId) !==
        String(
          this.partner.partnerId,
        )
    ) {
      return;
    }

    const presence =
      partnerPresenceFromProtocol(
        payload,
      ) || "gone";

    const previousPresence = this.partnerPresence;
    this.partnerPresence =
      presence;

    if (presence === "gone") {
      this._showPartnerGone();
    } else if (
      presence ===
      "reconnecting"
    ) {
      if (
        this.elements
          .qualityBanner
      ) {
        this.elements.qualityBanner.hidden =
          false;

        const name = this.partner?.displayName || "your partner";
        const notice = partnerLeftBanner(name);
        this.elements.qualityBanner.textContent = notice;
        this._setGuide(`Please wait while ${name} reconnects.`);
      }

      if (
        this.elements
          .connectionBadge
      ) {
        this.elements.connectionBadge.textContent =
          "PLEASE WAIT";

        this.elements.connectionBadge.className =
          "badge reconnecting";

        if (
          !this.elements
            .connectionBadge.dataset
        ) {
          this.elements.connectionBadge.dataset =
            {};
        }

        this.elements.connectionBadge.dataset.quality =
          "partner-reconnecting";
      }
    } else if (
      presence === "connected"
    ) {
      this._clearPartnerGone();
      if (previousPresence === "reconnecting" || previousPresence === "gone") {
        const name = this.partner?.displayName || "Your partner";
        this._setGuide(`${name} is back. Continue your date.`);
        if (this.elements.waitingBanner) {
          this.elements.waitingBanner.textContent = `${name} is back. You'll continue this date together.`;
          this.elements.waitingBanner.hidden = false;
        }
      }
    }
  }

  _onPartnerPresence(
    payload,
  ) {
    this._onPartnerState(
      payload,
    );
  }

  _clearPartnerGone() {
    if (this._partnerDisconnectWaitTimer) {
      clearTimeout(this._partnerDisconnectWaitTimer);
      this._partnerDisconnectWaitTimer = null;
    }

    if (
      this._partnerGoneTimer
    ) {
      clearTimeout(
        this._partnerGoneTimer,
      );

      this._partnerGoneTimer =
        null;
    }

    if (
      this.elements
        .partnerGoneBanner
    ) {
      this.elements.partnerGoneBanner.hidden =
        true;

      this.elements.partnerGoneBanner.textContent =
        "";
    }

    if (
      this.elements.qualityBanner &&
      !this._signalingDegraded &&
      !this.isPaused
    ) {
      this.elements.qualityBanner.hidden =
        true;

      this.elements.qualityBanner.textContent =
        "";
    }

    if (
      this.elements.statusFooter
    ) {
      this.elements.statusFooter.textContent =
        "You're on your date.";
    }

    if (
      this.elements
        .connectionBadge &&
      this.elements
        .connectionBadge
        .dataset?.quality ===
        "partner-gone"
    ) {
      this._applyConnectionQuality(
        this.negotiator?.pc
          ?.connectionState ||
          "connected",
      );
    }
  }

  _onOperatorPause(
    payload,
  ) {
    this.isPaused = true;

    this._stopTimer();

    const remainingSeconds =
      Number(
        payload.remaining_seconds ??
          0,
      );

    const remainingMs =
      remainingSeconds * 1000;

    const display =
      formatTimerDisplay(
        remainingMs,
      );

    this._renderTimer(
      display,
      TimerVisualState.PAUSED,
    );

    if (
      this.elements
        .qualityBanner
    ) {
      this.elements.qualityBanner.hidden =
        false;

      this.elements.qualityBanner.textContent =
        "Round temporarily paused by operator.";
    }

    if (this.onTimerTick) {
      this.onTimerTick({
        remainingMs,
        display,
        visual:
          TimerVisualState.PAUSED,
      });
    }
  }

  _onOperatorResume(
    payload,
  ) {
    this.isPaused = false;

    if (payload.ends_at) {
      this.roundEndTs =
        typeof payload.ends_at ===
        "number"
          ? payload.ends_at
          : Date.parse(
              payload.ends_at,
            );
    }

    if (
      this.elements
        .qualityBanner &&
      !this._signalingDegraded
    ) {
      this.elements.qualityBanner.hidden =
        true;

      this.elements.qualityBanner.textContent =
        "";
    }

    this._startTimer();
  }

  _onOperatorExtend(
    payload,
  ) {
    if (payload.ends_at) {
      this.roundEndTs =
        typeof payload.ends_at ===
        "number"
          ? payload.ends_at
          : Date.parse(
              payload.ends_at,
            );
    } else if (
      payload.extended_by_seconds &&
      this.roundEndTs
    ) {
      this.roundEndTs +=
        Number(
          payload.extended_by_seconds,
        ) * 1000;
    }

    if (!this.isPaused) {
      this._tickTimer();
    }
  }

  _createNegotiator(
    roomId,
    partnerId,
    rtcConfig,
  ) {
    const options = {
      myParticipantId:
        this.myParticipantId,

      partnerParticipantId:
        partnerId,

      roomId,

      selectedRoomId:
        this.mediaFallbackRoomId || roomId,

      fallbackRoomId:
        this.mediaFallbackRoomId,

      refreshRtcConfig:
        () => this._loadRtcConfig(),

      rtcConfig,

      forceFallback:
        this.forceMediaFallbackEnabled,

      localStream:
        this._sharedStream ||
        null,

      sendSignalingMessage:
        (signalMsg) => {
          if (
            this.ws?.readyState ===
            1
          ) {
            this.ws.send(
              JSON.stringify(
                signalMsg,
              ),
            );
          }
        },

      onRemoteStream:
        (stream) => {
          if (
            this.elements
              .remoteVideo &&
            this.elements
              .remoteVideo
              .srcObject !==
              stream
          ) {
            this.elements
              .remoteVideo
              .srcObject =
              stream;

            if (
              typeof this
                .elements
                .remoteVideo
                .play ===
              "function"
            ) {
              this.elements
                .remoteVideo
                .play()
                .catch(
                  () => {},
                );
            }
          }
        },

      onStateChange:
        (state) => {
          if (
            state === "closed" &&
            this.phase !==
              CallRoomPhase.EVENT_ENDED
          ) {
            return;
          }

          if (
            state === "failed"
          ) {
            this._schedulePermanentIceFailureNotice();
          } else if (
            state === "connected"
          ) {
            this._clearPermanentIceFailureTimer();
          }

          this._applyConnectionQuality(
            state,
          );
        },

      onFailure:
        (reason) => {
          this._clearPermanentIceFailureTimer();

          if (
            this._signalingDegraded &&
            !this._reconnectGaveUp
          ) {
            return;
          }

          this._onPartnerSwitchFailure(
            new Error(
              reason,
            ),
            reason,
          );
        },

      /*
       * T-39 / main branch escalation support.
       */
      escalationTimeoutMs:
        this._escalationTimeoutMs,

      scheduleEscalation:
        this._scheduleEscalation,
    };

    if (
      this._transientIceGraceMs !=
      null
    ) {
      options.transientIceGraceMs =
        this._transientIceGraceMs;
    }

    if (
      this._iceRestartAfterDisconnectMs !=
      null
    ) {
      options.iceRestartAfterDisconnectMs =
        this._iceRestartAfterDisconnectMs;
    }

    options.deferPermanentFailure =
      () =>
        this._signalingDegraded &&
        !this._reconnectGaveUp;

    let transport;

    /*
     * Preserve dependency injection used by tests
     * and by older code.
     */
    if (this._negotiatorFactory) {
      transport =
        this._negotiatorFactory(
          options,
        );
    } else {
      transport =
        new FailoverMediaTransport(
          options,
        );
    }

    /*
     * Compatibility shim for transports that expose
     * forceFallback() but not forceMediaFallback().
     */
    if (
      transport &&
      typeof transport
        .forceMediaFallback !==
        "function"
    ) {
      transport.forceMediaFallback =
        async () => {
          if (
            typeof transport
              .forceFallback ===
            "function"
          ) {
            return await transport.forceFallback();
          }

          return undefined;
        };
    }

    return transport;
  }

  _isSamePartnerAssignment() {
    if (
      !this.negotiator ||
      !this.partner
    ) {
      return false;
    }

    const pc =
      this.negotiator.pc;

    const pcUsable =
      pc &&
      pc.signalingState !==
        "closed" &&
      pc.connectionState !==
        "closed";

    return (
      pcUsable &&
      String(
        this.negotiator.roomId,
      ) ===
        String(
          this.partner.roomId,
        ) &&
      String(
        this.negotiator.partnerId,
      ) ===
        String(
          this.partner.partnerId,
        )
    );
  }

  _bindLocalPreview() {
    const stream =
      this.negotiator?.localStream ||
      this._sharedStream;

    if (
      this.elements.localVideo &&
      stream
    ) {
      if (
        this.elements.localVideo
          .srcObject !==
        stream
      ) {
        this.elements.localVideo.srcObject =
          stream;
      }
    }
  }

  _applyConnectionQuality(
    state,
  ) {
    if (
      this._signalingDegraded &&
      state !== "connected" &&
      state !== "relay"
    ) {
      this._showReconnecting();

      return;
    }

    if (
      state === "connected" ||
      state === "relay"
    ) {
      this._clearConnectionError();
    }

    if (
      this.elements.connectionBadge
    ) {
      const labels = {
        connecting:
          "CONNECTING",
        connected:
          "CONNECTED",
        degraded:
          "QUALITY DROP",
        failed:
          "FAILED",
        closed:
          "CLOSED",
        relay:
          "RELAY ACTIVE",
      };

      this.elements.connectionBadge.textContent =
        labels[state] ||
        String(
          state,
        ).toUpperCase();

      this.elements.connectionBadge.className =
        `badge ${state}`;

      if (
        !this.elements
          .connectionBadge
          .dataset
      ) {
        this.elements.connectionBadge.dataset =
          {};
      }

      this.elements.connectionBadge.dataset.quality =
        state;
    }

    if (
      this.elements.qualityBanner
    ) {
      const degraded =
        state === "degraded";

      this.elements.qualityBanner.hidden =
        !degraded;

      if (degraded) {
        this.elements.qualityBanner.textContent =
          "Temporary quality drop — recovering connection…";
      }
    }
  }

  _refreshQualityFromPeer() {
    const conn =
      this.negotiator?.pc
        ?.connectionState;

    if (
      conn === "connected"
    ) {
      this._applyConnectionQuality(
        "connected",
      );
    } else if (
      conn === "disconnected" ||
      conn === "failed"
    ) {
      this._applyConnectionQuality(
        "degraded",
      );
    }
  }

  _onSignalingClosed() {
    if (
      this._intentionalClose
    ) {
      return;
    }

    if (
      this.phase ===
      CallRoomPhase.EVENT_ENDED
    ) {
      return;
    }

    this._detachSocket();

    const keepRound =
      this.phase ===
        CallRoomPhase.IN_ROUND ||
      this.phase ===
        CallRoomPhase.PRECONNECTING ||
      this.phase ===
        CallRoomPhase.ROUND_ENDING;

    this.clockSync.stop();

    if (keepRound) {
      this._signalingDegraded =
        true;

      this._showReconnecting();
      this._scheduleReconnect();

      return;
    }

    this._setPhase(
      CallRoomPhase.DISCONNECTED,
    );

    this._stopTimer();
    this._showReconnecting();
    this._scheduleReconnect();
  }

  /**
   * Controller-level permanent ICE failure notice.
   *
   * We intentionally do not inspect the current pc.connectionState
   * inside the timer callback.
   *
   * The underlying transport may perform ICE restart/recovery and may
   * replace the underlying RTCPeerConnection while this timer is pending.
   *
   * A real "connected" callback clears this timer before it fires.
   */
  _schedulePermanentIceFailureNotice() {
    this._clearPermanentIceFailureTimer();

    if (
      this._transientIceGraceMs ==
      null
    ) {
      return;
    }

    const transport =
      this.negotiator;

    if (!transport) {
      return;
    }

    const delayMs =
      Math.max(
        0,
        Number(
          this._transientIceGraceMs,
        ) || 0,
      );

    const generation =
      ++this._permanentIceFailureGeneration;

    this._permanentIceFailureTimer =
      setTimeout(
        () => {
          this._permanentIceFailureTimer =
            null;

          /*
           * Ignore stale failure cycles.
           */
          if (
            generation !==
            this
              ._permanentIceFailureGeneration
          ) {
            return;
          }

          /*
           * Ignore old partner/transport instances.
           */
          if (
            this.negotiator !==
            transport
          ) {
            return;
          }

          /*
           * Never surface a permanent ICE failure
           * after the event has ended.
           */
          if (
            this.phase ===
            CallRoomPhase.EVENT_ENDED
          ) {
            return;
          }

          /*
           * Do NOT inspect transport.pc.connectionState here.
           *
           * The transport may restart/recreate the peer connection.
           */
          this._onPartnerSwitchFailure(
            new Error(
              "ICE connection failed",
            ),
            "ICE_CONNECTION_FAILED",
          );
        },
        delayMs,
      );

    if (
      this._permanentIceFailureTimer
        ?.unref
    ) {
      this._permanentIceFailureTimer.unref();
    }
  }

  _clearPermanentIceFailureTimer() {
    if (
      this._permanentIceFailureTimer
    ) {
      clearTimeout(
        this._permanentIceFailureTimer,
      );

      this._permanentIceFailureTimer =
        null;
    }

    this._permanentIceFailureGeneration +=
      1;
  }

  _onPartnerSwitchFailure(
    err,
    reason =
      "PARTNER_SWITCH_FAILED",
  ) {
    console.error(
      "[CallRoom] Partner switch/renegotiation failed:",
      err,
    );

    let message =
      "Could not connect to the next partner. Your camera stays on and other rounds are not stopped.";

    if (
      reason ===
      "ICE_CONNECTION_FAILED"
    ) {
      message =
        "Connection to this partner failed. Your camera stays on; other rounds are not stopped.";
    }

    this._showConnectionError(
      message,
    );

    if (
      this.elements.connectionBadge
    ) {
      this.elements.connectionBadge.textContent =
        "Connection problem";

      this.elements.connectionBadge.className =
        "badge failed";

      if (
        !this.elements
          .connectionBadge.dataset
      ) {
        this.elements.connectionBadge.dataset =
          {};
      }

      this.elements.connectionBadge.dataset.quality =
        "failed";
    }

    this._bindLocalPreview();
  }

  _showConnectionError(
    message,
  ) {
    if (
      this.elements.errorBanner
    ) {
      this.elements.errorBanner.hidden =
        false;

      this.elements.errorBanner.textContent =
        message;
    }
  }

  _clearConnectionError() {
    if (
      this.elements.errorBanner
    ) {
      this.elements.errorBanner.hidden =
        true;

      this.elements.errorBanner.textContent =
        "";
    }
  }

  async _loadRtcConfig() {
    if (!this._fetchIceServers) {
      return {};
    }

    try {
      const ice =
        await this._fetchIceServers();

      return {
        iceServers:
          ice.iceServers || [],
      };
    } catch {
      return {};
    }
  }

  _startTimer() {
    this._stopTimer();

    this._timerInterval =
      setInterval(
        () =>
          this._tickTimer(),
        250,
      );

    if (
      this._timerInterval?.unref
    ) {
      this._timerInterval.unref();
    }

    this._tickTimer();
  }

  _stopTimer() {
    if (
      this._timerInterval
    ) {
      clearInterval(
        this._timerInterval,
      );

      this._timerInterval =
        null;
    }
  }

  _tickTimer() {
    if (this.isPaused) {
      return;
    }

    if (
      !this.roundEndTs ||
      !this.clockSync.isSynchronised
    ) {
      this._renderTimer(
        "--:--",
        TimerVisualState.WAITING,
      );

      return;
    }

    const remainingMs =
      computeRemainingMs(
        this.roundEndTs,
        this.clockSync.offsetMs,
        Date.now(),
      );

    const visual =
      timerVisualState(
        remainingMs,
        this.warningThresholdSeconds,
        this.serverWarningActive,
        this.isPaused,
      );

    const display =
      formatTimerDisplay(
        remainingMs,
      );

    this._renderTimer(
      display,
      visual,
    );

    if (
      remainingMs <= 0 &&
      this.partner &&
      !this._dateClockEnded &&
      this.phase === CallRoomPhase.IN_ROUND
    ) {
      this._dateClockEnded = true;
      void this._onRoundEnd({
        round_number: this.roundNumber,
        break_duration_seconds: 0,
      });
    }

    if (this.onTimerTick) {
      this.onTimerTick({
        remainingMs,
        display,
        visual,
      });
    }
  }

  _renderTimer(
    display,
    visual,
  ) {
    if (
      this.elements.timerDisplay
    ) {
      if (this.elements.timerDisplay.textContent !== display) {
        this.elements.timerDisplay.textContent = display;
      }

      if (this.elements.timerDisplay.dataset.state !== visual) {
        this.elements.timerDisplay.dataset.state = visual;
      }
    }

    if (
      this.elements.timerContainer
    ) {
      if (this.elements.timerContainer.dataset.state !== visual) {
        this.elements.timerContainer.dataset.state = visual;
      }
    }
  }

  _updatePartnerUI() {
    if (
      this.elements.partnerName
    ) {
      this.elements.partnerName.textContent =
        this.partner?.displayName ??
        "Partner";
    }

    if (
      this.elements.partnerTags
    ) {
      const tags =
        this.partner?.tags ?? [];

      this.elements.partnerTags.textContent =
        tags.length
          ? tags.join(", ")
          : "";

      this.elements.partnerTags.hidden =
        !tags.length;
    }

    if (
      this.elements.roundLabel
    ) {
      this.elements.roundLabel.textContent =
        this.roundNumber !=
        null
          ? `Round ${this.roundNumber}`
          : "—";
    }

    if (
      this.elements.roomLabel
    ) {
      this.elements.roomLabel.textContent =
        this.partner?.roomId ??
        "—";
    }
  }

  _setPhase(phase) {
    this.phase =
      phase;

    if (
      this.elements.phaseBadge
    ) {
      this.elements.phaseBadge.textContent =
        phase
          .replace(
            /_/g,
            " ",
          )
          .toUpperCase();

      this.elements.phaseBadge.dataset.phase =
        phase;
    }

    if (this.onPhaseChange) {
      this.onPhaseChange(
        phase,
      );
    }
  }

  _scheduleReconnect() {
    if (
      this.phase ===
      CallRoomPhase.EVENT_ENDED
    ) {
      return;
    }

    this._clearReconnect();
    this._clearEarlyExitTimer();
    this._clearStandbyCountdown();
    this._clearAutoReload();

    const now =
      this._now();

    if (
      this._reconnectStartedAt ==
      null
    ) {
      this._reconnectStartedAt =
        now;
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

    const delay =
      nextReconnectDelayMs(
        this._reconnectAttempt,
        this._reconnectInitialDelayMs,
        this._reconnectMaxDelayMs,
      );

    this._reconnectAttempt +=
      1;

    this._reconnectTimer =
      setTimeout(
        () => {
          this._reconnectTimer =
            null;

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
        },
        delay,
      );

    if (
      this._reconnectTimer?.unref
    ) {
      this._reconnectTimer.unref();
    }
  }

  _giveUpReconnect() {
    this._reconnectGaveUp =
      true;

    this._clearReconnect();
    this._detachSocket();

    this._showConnectionError(
      "Could not restore the connection. The round was not ended for other participants.",
    );

    if (
      this.elements.connectionBadge
    ) {
      this.elements.connectionBadge.textContent =
        "FAILED";

      this.elements.connectionBadge.className =
        "badge failed";

      if (
        !this.elements
          .connectionBadge.dataset
      ) {
        this.elements.connectionBadge.dataset =
          {};
      }

      this.elements.connectionBadge.dataset.quality =
        "failed";
    }

    if (
      this.phase === CallRoomPhase.ROUND_ENDING ||
      this._activeInterestPromptId !== null
    ) {
      return;
    }
    this._startAutoReload(5);
  }

  _showReconnecting() {
    if (
      this.elements.connectionBadge
    ) {
      this.elements.connectionBadge.textContent =
        "RECONNECTING";

      this.elements.connectionBadge.className =
        "badge reconnecting";

      if (
        !this.elements
          .connectionBadge.dataset
      ) {
        this.elements.connectionBadge.dataset =
          {};
      }

      this.elements.connectionBadge.dataset.quality =
        "reconnecting";
    }

    if (
      this.elements.qualityBanner
    ) {
      this.elements.qualityBanner.hidden =
        false;

      this.elements.qualityBanner.textContent =
        "RECONNECTING — restoring the same round and partner…";
    }

    if (this.elements.disconnectNotificationBanner) {
      this.elements.disconnectNotificationBanner.textContent =
        "You got disconnected. Please refresh the page if your connection does not restore.";
      this.elements.disconnectNotificationBanner.hidden = false;
    }

    if (
      this.elements.statusFooter
    ) {
      this.elements.statusFooter.textContent =
        "Please wait. We're restoring your date.";
    }
  }

  _bindOnlineListener() {
    if (this._onlineHandler) {
      return;
    }

    if (
      typeof window ===
        "undefined" ||
      typeof window.addEventListener !==
        "function"
    ) {
      return;
    }

    this._onlineHandler =
      () => {
        if (
          this.phase ===
          CallRoomPhase.EVENT_ENDED
        ) {
          return;
        }

        if (
          !this._signalingDegraded &&
          this.phase !==
            CallRoomPhase.DISCONNECTED
        ) {
          return;
        }

        this._reconnectGaveUp =
          false;

        this._reconnectAttempt =
          0;

        this._reconnectStartedAt =
          this._now();

        this._clearReconnect();
      this._clearEarlyExitTimer();
      this._clearStandbyCountdown();
      this._clearAutoReload();

        this.connect();
      };

    window.addEventListener(
      "online",
      this._onlineHandler,
    );
  }

  _unbindOnlineListener() {
    if (!this._onlineHandler) {
      return;
    }

    if (
      typeof window !==
        "undefined" &&
      typeof window.removeEventListener ===
        "function"
    ) {
      window.removeEventListener(
        "online",
        this._onlineHandler,
      );
    }

    this._onlineHandler =
      null;
  }

  _bindBeforeUnloadListener() {
    if (
      typeof window ===
        "undefined" ||
      typeof window.addEventListener !==
        "function"
    ) {
      return;
    }

    if (
      this._beforeUnloadHandler
    ) {
      return;
    }

    this._beforeUnloadHandler =
      () => {
        if (
          this.ws &&
          this.ws.readyState ===
            1
        ) {
          try {
            this.ws.send(
              JSON.stringify({
                type:
                  "client.telemetry",
                payload: {
                  cause:
                    "tab_closed",
                },
              }),
            );
          } catch {}
        }
      };

    window.addEventListener(
      "beforeunload",
      this._beforeUnloadHandler,
    );
  }

  _unbindBeforeUnloadListener() {
    if (
      typeof window ===
        "undefined" ||
      !this._beforeUnloadHandler
    ) {
      return;
    }

    if (
      typeof window.removeEventListener ===
      "function"
    ) {
      window.removeEventListener(
        "beforeunload",
        this._beforeUnloadHandler,
      );
    }

    this._beforeUnloadHandler =
      null;
  }

  _clearReconnect() {
    if (
      this._reconnectTimer
    ) {
      clearTimeout(
        this._reconnectTimer,
      );

      this._reconnectTimer =
        null;
    }
  }

  _startAutoReload(seconds = 5) {
    if (this._autoReloadTimer) {
      clearInterval(this._autoReloadTimer);
      this._autoReloadTimer = null;
    }
    let remaining = Number(seconds) || 5;
    const renderBanner = () => {
      if (this.elements.disconnectNotificationBanner) {
        this.elements.disconnectNotificationBanner.innerHTML = `
          <span>Connection dropped. Reconnecting automatically in <strong>${remaining}s</strong>...</span>
          <button type="button" id="meetakkRefreshNowBtn" style="margin-left: 14px; padding: 4px 12px; background: #fff; color: #b91c1c; font-weight: 700; border: 1px solid #b91c1c; border-radius: 4px; cursor: pointer;">Refresh Now</button>
        `;
        this.elements.disconnectNotificationBanner.hidden = false;
        const btn = this.elements.disconnectNotificationBanner.querySelector('#meetakkRefreshNowBtn');
        if (btn) {
          btn.onclick = () => {
            if (typeof window !== "undefined" && window.location) {
              window.location.reload();
            }
          };
        }
      }
    };
    renderBanner();
    this._autoReloadTimer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        clearInterval(this._autoReloadTimer);
        this._autoReloadTimer = null;
        if (typeof window !== "undefined" && window.location) {
          window.location.reload();
        }
      } else {
        renderBanner();
      }
    }, 1000);
  }

  _clearAutoReload() {
    if (this._autoReloadTimer) {
      clearInterval(this._autoReloadTimer);
      this._autoReloadTimer = null;
    }
  }

  _startEarlyExitTimer() {
    this._clearEarlyExitTimer();
    const waitMs = Math.max(1, (this.earlyExitMinMinutes || 3)) * 60 * 1000;
    this._leaveDateTimer = setTimeout(() => {
      if (this.elements.leaveDateWrapper && this.phase === CallRoomPhase.IN_ROUND) {
        this.elements.leaveDateWrapper.hidden = false;
      }
    }, waitMs);
  }

  _clearEarlyExitTimer() {
    if (this._leaveDateTimer) {
      clearTimeout(this._leaveDateTimer);
      this._leaveDateTimer = null;
    }
    if (this.elements.leaveDateWrapper) {
      this.elements.leaveDateWrapper.hidden = true;
    }
    if (this.elements.leaveDateConfirmModal) {
      this.elements.leaveDateConfirmModal.hidden = true;
    }
  }

  async leaveDateEarly() {
    this._clearEarlyExitTimer();
    if (this.elements.leaveDateConfirmModal) {
      this.elements.leaveDateConfirmModal.hidden = true;
    }
    if (this.elements.qualityBanner) {
      this.elements.qualityBanner.hidden = true;
    }
    const roomId = this.partner?.roomId || this.roomId;
    const partnerName = this.partner?.displayName || "your partner";
    const roundNum = Number(this.roundNumber) || 1;
    const nowTs = this._now();
    const durationSec = this.roundStartTs ? (nowTs - this.roundStartTs) / 1000 : 0;
    let left = false;

    if (roomId) {
      try {
        const csrfToken =
          document.querySelector("[name=csrfmiddlewaretoken]")?.value ||
          (document.cookie.match(/csrftoken=([^;]+)/)?.[1] || "");
        const response = await fetch("/api/leave-date/", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": csrfToken,
          },
          body: JSON.stringify({ room_id: roomId, reason: "early_exit" }),
        });
        left = response.ok;
      } catch (e) {
        left = false;
      }
    }

    if (this.negotiator) {
      try {
        await this.negotiator.endRound?.();
      } catch (e) {}
    }
    if (this.elements.remoteVideo) {
      this.elements.remoteVideo.srcObject = null;
    }
    this._bindLocalPreview();
    this.partner = null;
    this._updatePartnerUI();

    try {
      sessionStorage.removeItem("meetakk_active_date");
    } catch (e) {}

    const minSec = (this.earlyExitMinMinutes || 3) * 60;
    if (left && durationSec >= minSec && roomId) {
      const promptId = `${roundNum}:${roomId}`;
      if (!this._interestPrompts.has(promptId)) {
        const prompt = {
          round_number: roundNum,
          room_id: roomId,
          partner_display_name: partnerName,
          server_ts: nowTs,
          deadline_ts: nowTs + 60000,
          own_response: null,
          mutual: false,
        };
        this._interestPrompts.set(promptId, prompt);
        this._renderInterestPrompt(promptId, prompt);
        if (this.elements.interestTitle) {
          this.elements.interestTitle.textContent = `You left your date with ${partnerName}. Would you like to stay in touch with them?`;
        }
      }
    }

    this._setPhase(CallRoomPhase.IDLE);
    if (this._activeInterestPromptId === null) {
      this._onServerWaiting({ reason: "waiting_for_partner" });
    }
  }

  _startStandbyCountdown(totalSeconds = 420) {
    this._clearStandbyCountdown();
    let remaining = Number(totalSeconds) || 420;
    const formatTime = (secs) => {
      const m = Math.floor(secs / 60);
      const s = Math.floor(secs % 60);
      return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
    };
    if (this.elements.standbyTimerCountdown) {
      this.elements.standbyTimerCountdown.textContent = formatTime(remaining);
    }
    this._standbyTimer = setInterval(() => {
      remaining -= 1;
      if (remaining <= 0) {
        this._clearStandbyCountdown();
      } else if (this.elements.standbyTimerCountdown) {
        this.elements.standbyTimerCountdown.textContent = formatTime(remaining);
      }
    }, 1000);
  }

  _clearStandbyCountdown() {
    if (this._standbyTimer) {
      clearInterval(this._standbyTimer);
      this._standbyTimer = null;
    }
    if (this.elements.mainRoomStandbyBanner) {
      this.elements.mainRoomStandbyBanner.hidden = true;
    }
  }
}
