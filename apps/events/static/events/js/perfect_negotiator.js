/**
 * WebRTC Perfect Negotiation & Media Transport Lifecycle (T-27, T-31, T-32, T-37)
 * Enforces:
 *  - 640x360 target resolution
 *  - 500 kbps video bitrate cap via RTCRtpSender.setParameters
 *  - Full lifecycle transitions (idle -> preconnected -> open -> closed)
 *  - Seamless round switching (switchPartner) reusing hardware tracks
 *  - getUserMedia once per session; partner switch never re-requests devices
 *  - Transient ICE/network recovery without tearing down the room (T-32)
 *  - Deterministic W3C Perfect Negotiation with rollback
 *  - Camera/microphone acquisition and reuse for every round
 *  - Direct vs Relay connection path and establishment telemetry reporting (T-37)
 */

export const DEFAULT_MEDIA_CONSTRAINTS = {
  video: {
    width: { ideal: 640, max: 640 },
    height: { ideal: 360, max: 360 },
    frameRate: { ideal: 30, max: 30 },
  },
  audio: {
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  },
};

export const VIDEO_MAX_BITRATE_BPS = 500_000; // 500 kbps cap

export const TransportState = Object.freeze({
  IDLE: "idle",
  PRECONNECTED: "preconnected",
  OPEN: "open",
  CLOSED: "closed",
});

/** Wait this long after ICE `failed` before treating it as permanent (T-32). */
export const TRANSIENT_ICE_GRACE_MS = 5000;

/** Let brief `disconnected` blips recover before requesting ICE restart. */
export const ICE_RESTART_AFTER_DISCONNECT_MS = 2000;

export const ConnectionQuality = Object.freeze({
  CONNECTING: "connecting",
  CONNECTED: "connected",
  DEGRADED: "degraded",
  FAILED: "failed",
  CLOSED: "closed",
});

let sharedLocalStream = null;
let sharedAcquirePromise = null;
let sharedGetUserMediaCalls = 0;
function streamHasLiveTracks(stream) {
  return Boolean(
    stream &&
      stream.getAudioTracks?.().some((track) => track.readyState !== "ended") &&
      stream.getVideoTracks?.().some((track) => track.readyState !== "ended"),
  );
}

export function getSharedGetUserMediaCallCount() {
  return sharedGetUserMediaCalls;
}

export function releaseSharedLocalMedia() {
  if (sharedLocalStream) {
    sharedLocalStream.getTracks().forEach((track) => {
      try {
        track.stop();
      } catch {
        // ignore
      }
    });
  }
  sharedLocalStream = null;
  sharedAcquirePromise = null;
}

export function resetSharedLocalMediaForTests() {
  releaseSharedLocalMedia();
  sharedGetUserMediaCalls = 0;
}

/**
 * Acquire camera and microphone once and reuse them for every round.
 * Subsequent callers receive the live stream without calling getUserMedia.
 */
export async function acquireSharedLocalMedia(
  constraints = DEFAULT_MEDIA_CONSTRAINTS,
) {
  if (streamHasLiveTracks(sharedLocalStream)) {
    return sharedLocalStream;
  }
  if (sharedLocalStream) {
    sharedLocalStream.getTracks().forEach((track) => {
      try { track.stop(); } catch {}
    });
    sharedLocalStream = null;
    sharedAcquirePromise = null;
  }
  if (!sharedAcquirePromise) {
    sharedAcquirePromise = (async () => {
      sharedGetUserMediaCalls += 1;
      let stream;
      try {
        stream = await navigator.mediaDevices.getUserMedia(constraints);
        const audioTrack = stream.getAudioTracks?.().find((track) => track.readyState !== "ended");
        const videoTrack = stream.getVideoTracks?.().find((track) => track.readyState !== "ended");
        if (!audioTrack || !videoTrack) {
          throw new Error("Camera and microphone are both required.");
        }
        const settings = videoTrack.getSettings?.() || {};
        if (settings.width <= 2 && settings.height <= 2) {
          throw new Error("Camera returned an unusable video track.");
        }
        sharedLocalStream = stream;
        return stream;
      } catch (err) {
        stream?.getTracks?.().forEach((track) => {
          try { track.stop(); } catch {}
        });
        sharedAcquirePromise = null;
        throw err;
      }
    })();
  }
  return sharedAcquirePromise;
}

export class PerfectNegotiator {
  constructor({
    myParticipantId,
    partnerParticipantId,
    roomId,
    sendSignalingMessage,
    rtcConfig = {},
    onRemoteStream = null,
    onStateChange = null,
    onStats = null,
    onFailure = null,
    localStream = null,
    transientIceGraceMs = TRANSIENT_ICE_GRACE_MS,
    iceRestartAfterDisconnectMs = ICE_RESTART_AFTER_DISCONNECT_MS,
    deferPermanentFailure = null,
  }) {
    this.myId = String(myParticipantId);
    this.partnerId = String(partnerParticipantId);
    this.roomId = roomId;
    this.sendSignal = sendSignalingMessage;
    this.rtcConfig = rtcConfig;
    this.adapterKind =
      this.rtcConfig && this.rtcConfig.iceTransportPolicy === "relay"
        ? "relay"
        : "direct";

    // Callbacks
    this.onRemoteStream = onRemoteStream;
    this.onStateChange = onStateChange;
    this.onStats = onStats;
    this.onFailure = onFailure;

    // Deterministic Perfect Negotiation role (W3C standard)
    this.isPolite = this.myId < this.partnerId;

    this.isMakingOffer = false;
    this.ignoreOffer = false;
    this.isMuted = false;
    this.state = TransportState.IDLE;
    this.quality = ConnectionQuality.CONNECTING;

    this.localStream = null;
    this.remoteStream = new MediaStream();
    this.statsInterval = null;
    this._lastBytesSent = 0;
    this._lastStatsTimestamp = 0;
    this._transientIceGraceMs = transientIceGraceMs;
    this._iceRestartAfterDisconnectMs = iceRestartAfterDisconnectMs;
    this._iceRestartInFlight = false;
    this._iceRestartTimer = null;
    this._permanentFailureTimer = null;
    this._deferPermanentFailure = deferPermanentFailure;
    // Trickle ICE can arrive before the matching offer/answer has been applied.
    this._pendingRemoteCandidates = [];
    this._mediaReceivedReported = false;
    this._mediaReportInFlight = false;
    this._mediaReportAbort = null;

    // T-37 Telemetry Tracking
    this._connectionStartTime = Date.now();
    this._telemetryReported = false;

    if (localStream) {
      this.localStream = localStream;
      if (!streamHasLiveTracks(sharedLocalStream)) {
        sharedLocalStream = localStream;
      }
    }

    this._initPeerConnection();
  }

  _initPeerConnection() {
    this._connectionStartTime = Date.now();
    this._telemetryReported = false;
    this._mediaReceivedReported = false;
    this._mediaReportInFlight = false;
    this._mediaReportAbort = null;
    this.pc = new RTCPeerConnection(this.rtcConfig);
    this._setupPeerEvents();
  }

  _setupPeerEvents() {
    const pc = this.pc;

    // 1. Perfect Negotiation Offer Generation
    pc.onnegotiationneeded = async () => {
      if (this.pc !== pc) return;
      if (this.isMakingOffer || pc.signalingState !== "stable") return;
      try {
        this.isMakingOffer = true;
        await pc.setLocalDescription();
        if (this.pc !== pc) return;
        this.sendSignal({
          type: "client.webrtc.offer",
          version: 1,
          payload: {
            room_id: this.roomId,
            sdp: pc.localDescription.sdp,
          },
        });
      } catch (err) {
        console.error("[WebRTC] Negotiation offer error:", err);
      } finally {
        if (this.pc === pc) this.isMakingOffer = false;
      }
    };

    // 2. Trickle ICE
    pc.onicecandidate = ({ candidate }) => {
      if (this.pc !== pc) return;
      if (candidate && candidate.candidate) {
        this.sendSignal({
          type: "client.webrtc.ice_candidate",
          version: 1,
          payload: {
            room_id: this.roomId,
            candidate: candidate.candidate,
            sdp_mid: candidate.sdpMid ?? "0",
            sdp_mline_index: candidate.sdpMLineIndex ?? 0,
          },
        });
      }
    };

    // 3. Remote Tracks
    pc.ontrack = (event) => {
      if (this.pc !== pc) return;
      if (event.streams && event.streams[0]) {
        this.remoteStream = event.streams[0];
      } else if (event.track) {
        this.remoteStream.addTrack(event.track);
      }
      if (this.onRemoteStream) {
        this.onRemoteStream(this.remoteStream);
      }
    };

    // 4. Connection State & Degradation Monitoring (T-32 & T-37)
    pc.onconnectionstatechange = () => {
      if (this.pc !== pc) return;
      this._onPeerConnectionState(pc.connectionState);
    };
  }

  _onPeerConnectionState(connState) {
    console.log(`[WebRTC] Connection state: ${connState}`);

    if (connState === "connected") {
      this._clearRecoveryTimers();
      this._iceRestartInFlight = false;
      this._startStatsMonitor();
      this._emitQuality(ConnectionQuality.CONNECTED);
      void this._applyVideoBitrateCap();

      // T-37: Report Direct vs Relay path telemetry once connection is established
      if (!this._telemetryReported) {
        this._telemetryReported = true;
        void reportConnectionTelemetry(this.pc, this.roomId, this._connectionStartTime);
      }
      return;
    }

    if (connState === "disconnected") {
      this._stopStatsMonitor();
      this._emitQuality(ConnectionQuality.DEGRADED);
      this._scheduleIceRestart();
      return;
    }

    if (connState === "failed") {
      this._stopStatsMonitor();
      this._emitQuality(ConnectionQuality.DEGRADED);
      this._maybeRestartIce();
      this._schedulePermanentFailure();
      return;
    }

    if (connState === "closed") {
      this._clearRecoveryTimers();
      this._stopStatsMonitor();
      this._cancelMediaReceiptReport();
      this._emitQuality(ConnectionQuality.CLOSED);
    }
  }

  _emitQuality(quality) {
    this.quality = quality;
    if (this.onStateChange) this.onStateChange(quality);
  }

  _scheduleIceRestart() {
    if (this._iceRestartTimer || this._iceRestartInFlight) return;
    this._iceRestartTimer = setTimeout(() => {
      this._iceRestartTimer = null;
      if (!this.pc) return;
      if (this.pc.connectionState === "connected" || this.pc.connectionState === "closed") {
        return;
      }
      this._maybeRestartIce();
    }, this._iceRestartAfterDisconnectMs);
  }

  _maybeRestartIce() {
    if (!this.pc || this._iceRestartInFlight) return;
    if (this.state === TransportState.CLOSED) return;
    const conn = this.pc.connectionState;
    if (conn === "connected" || conn === "closed") return;

    this._iceRestartInFlight = true;
    try {
      if (typeof this.pc.restartIce === "function") {
        this.pc.restartIce();
        console.log("[WebRTC] ICE restart requested for transient recovery");
        return;
      }
      void this._legacyIceRestartOffer();
    } catch (err) {
      this._iceRestartInFlight = false;
      console.warn("[WebRTC] ICE restart failed:", err);
    }
  }

  async _legacyIceRestartOffer() {
    const pc = this.pc;
    if (!pc) {
      this._iceRestartInFlight = false;
      return;
    }
    try {
      this.isMakingOffer = true;
      const offer = await pc.createOffer({ iceRestart: true });
      if (this.pc !== pc) return;
      await pc.setLocalDescription(offer);
      if (this.pc !== pc) return;
      this.sendSignal({
        type: "client.webrtc.offer",
        version: 1,
        payload: {
          room_id: this.roomId,
          sdp: this.pc.localDescription.sdp,
        },
      });
    } catch (err) {
      console.warn("[WebRTC] ICE restart offer failed:", err);
      if (this.pc === pc) this._iceRestartInFlight = false;
    } finally {
      if (this.pc === pc) this.isMakingOffer = false;
    }
  }

  _schedulePermanentFailure() {
    if (this._permanentFailureTimer) return;
    this._permanentFailureTimer = setTimeout(() => {
      this._permanentFailureTimer = null;
      if (
        typeof this._deferPermanentFailure === "function" &&
        this._deferPermanentFailure()
      ) {
        this._schedulePermanentFailure();
        return;
      }
      const conn = this.pc?.connectionState;
      if (conn !== "failed" && conn !== "disconnected") return;
      this._stopStatsMonitor();
      this._emitQuality(ConnectionQuality.FAILED);
      if (this.onFailure) this.onFailure("ICE_CONNECTION_FAILED");
    }, this._transientIceGraceMs);
  }

  /**
   * After T-14 signaling returns, reuse T-32 ICE restart on the same PC.
   * Does not create a peer connection or call getUserMedia.
   */
  recoverAfterSignalingRestore() {
    if (!this.pc || this.state === TransportState.CLOSED) return;
    const conn = this.pc.connectionState;
    if (conn === "connected") {
      this._clearRecoveryTimers();
      this._iceRestartInFlight = false;
      this._emitQuality(ConnectionQuality.CONNECTED);
      return;
    }
    if (conn === "closed") return;
    this._iceRestartInFlight = false;
    this._emitQuality(ConnectionQuality.DEGRADED);
    this._maybeRestartIce();
  }

  _clearRecoveryTimers() {
    if (this._iceRestartTimer) {
      clearTimeout(this._iceRestartTimer);
      this._iceRestartTimer = null;
    }
    if (this._permanentFailureTimer) {
      clearTimeout(this._permanentFailureTimer);
      this._permanentFailureTimer = null;
    }
  }

  /**
   * Close the current RTCPeerConnection only. Local MediaStream/tracks
   * stay alive for the next round (T-31).
   */
  _teardownPeerConnection() {
    this._clearRecoveryTimers();
    this._iceRestartInFlight = false;
    this._stopStatsMonitor();
    this._cancelMediaReceiptReport();
    this._telemetryReported = false;
    const pc = this.pc;
    if (!pc) return;

    pc.onnegotiationneeded = null;
    pc.onicecandidate = null;
    pc.ontrack = null;
    pc.onconnectionstatechange = null;

    try {
      pc.getSenders().forEach((sender) => {
        try {
          pc.removeTrack(sender);
        } catch {
          // ignore
        }
      });
    } catch {
      // ignore
    }

    try {
      pc.close();
    } catch {
      // ignore
    }
    this.pc = null;
  }

  /**
   * Close the peer connection and drop listeners without stopping tracks.
   * Used when replacing this adapter with another T-26 transport (T-38).
   */
  detachPeerKeepMedia() {
    this._teardownPeerConnection();
    this.remoteStream = new MediaStream();
    this.onRemoteStream = null;
    this.onFailure = null;
    this.onStateChange = null;
    this.onStats = null;
    this.sendSignal = () => {};
    this.state = TransportState.CLOSED;
  }

  /**
   * Acquire camera and microphone media.
   * Never re-prompts when a live shared stream already exists (T-31).
   */
  async acquireLocalMedia(constraints = DEFAULT_MEDIA_CONSTRAINTS) {
    if (!streamHasLiveTracks(this.localStream)) {
      this.localStream = await acquireSharedLocalMedia(constraints);
    }
    this._attachTracksToPC();
    return this.localStream;
  }

  _attachTracksToPC() {
    if (!this.localStream || !this.pc) return;
    const attached = new Set(
      this.pc.getSenders().map((sender) => sender.track).filter(Boolean),
    );
    this.localStream.getTracks().forEach((track) => {
      if (attached.has(track)) return;
      const sender = this.pc.addTrack(track, this.localStream);
      if (sender) sender._trackKind = track.kind;
    });
  }

  /**
   * Pre-connection phase: attaches tracks muted in background before round start.
   */
  async preconnect(roomId, partnerParticipantId) {
    this.roomId = roomId;
    this.partnerId = String(partnerParticipantId);
    this.isPolite = this.myId < this.partnerId;

    this.isMuted = true;
    this.state = TransportState.PRECONNECTED;
    await this._applySenderMuteState();
  }

  /**
   * Open / Start Round: un-mutes tracks and enforces the 500 kbps cap.
   */
  async open() {
    this.isMuted = false;
    this.state = TransportState.OPEN;
    await this._applySenderMuteState();
    await this._applyVideoBitrateCap();
  }

  /**
   * Mutes or un-mutes sending without stopping hardware capture devices.
   * Source tracks stay enabled so the local preview remains visible (T-31).
   */
  setMediaMuted(isMuted) {
    this.isMuted = isMuted;
    void this._applySenderMuteState();
  }

  async _applySenderMuteState() {
    if (!this.pc || typeof this.pc.getSenders !== "function") return;

    const sourceByKind = {};
    if (this.localStream) {
      this.localStream.getTracks().forEach((track) => {
        sourceByKind[track.kind] = track;
      });
    }

    for (const sender of this.pc.getSenders()) {
      const kind = sender.track?.kind || sender._trackKind;
      if (!kind) continue;
      sender._trackKind = kind;
      const source = sourceByKind[kind];
      if (typeof sender.replaceTrack !== "function") continue;
      try {
        if (this.isMuted) {
          if (sender.track) await sender.replaceTrack(null);
        } else if (source && sender.track !== source) {
          await sender.replaceTrack(source);
        }
      } catch (err) {
        console.warn("[WebRTC] Sender mute/unmute failed:", err);
      }
    }
  }

  /**
   * Enforces 500 kbps maxBitrate on video sender via RTCRtpSender.setParameters.
   */
  async _applyVideoBitrateCap() {
    if (!this.pc) return;
    const videoSender = this.pc
      .getSenders()
      .find((s) => s.track && s.track.kind === "video");

    if (!videoSender) return;

    let timeout;
    try {
      const params = videoSender.getParameters();
      if (!params.encodings || params.encodings.length === 0) {
        params.encodings = [{}];
      }
      params.encodings[0].maxBitrate = VIDEO_MAX_BITRATE_BPS;
      // This optional optimization must never block signaling or round changes
      // if a browser leaves setParameters pending during peer replacement.
      await Promise.race([
        videoSender.setParameters(params),
        new Promise((_, reject) => {
          timeout = setTimeout(() => reject(new Error("Bitrate update timed out")), 1000);
        }),
      ]);
      console.log(`[WebRTC] Video bitrate cap enforced at ${VIDEO_MAX_BITRATE_BPS / 1000} kbps`);
    } catch (err) {
      console.warn("[WebRTC] Failed to set video maxBitrate:", err);
    } finally {
      clearTimeout(timeout);
    }
  }

  /**
   * Manual offer generation triggered by UI button or round transitions.
   */
  async createManualOffer() {
    const pc = this.pc;
    if (!pc) return;
    // Track attachment raises negotiationneeded asynchronously. Reuse that
    // offer when it already won the race instead of dispatching a second SDP.
    if (this.isMakingOffer || pc.signalingState !== "stable") return;
    try {
      this.isMakingOffer = true;
      const offer = await pc.createOffer({ offerToReceiveVideo: true, offerToReceiveAudio: true });
      if (this.pc !== pc) return;
      await pc.setLocalDescription(offer);
      if (this.pc !== pc) return;
      this.sendSignal({
        type: "client.webrtc.offer",
        version: 1,
        payload: {
          room_id: this.roomId,
          sdp: this.pc.localDescription.sdp,
        },
      });
      console.log("[WebRTC] Manual offer dispatched successfully.");
    } catch (err) {
      console.error("[WebRTC] Failed to create manual offer:", err);
    } finally {
      if (this.pc === pc) this.isMakingOffer = false;
    }
  }

  async enableRelay(rtcConfig = this.rtcConfig) {
    this.rtcConfig = { ...rtcConfig, iceTransportPolicy: "relay" };
    const pc = this.pc;
    if (!pc || typeof pc.setConfiguration !== "function") return false;
    // Retain the peer's SDP and transceivers while changing ICE policy. Closing
    // and replacing it here can strand in-flight SDP operations on the old PC.
    pc.setConfiguration(this.rtcConfig);
    pc.restartIce();
    return true;
  }

  /**
   * Prepare for a new round during preconnect: teardown previous peer
   * connection, re-init with new partner, but stay muted until open().
   * Reuses the existing local MediaStream; never calls getUserMedia.
   */
  async prepareRound(newRoomId, newPartnerId) {
    try {
      this._teardownPeerConnection();

      this.roomId = newRoomId;
      this.partnerId = String(newPartnerId);
      this.isPolite = this.myId < this.partnerId;
      this.remoteStream = new MediaStream();
      this._pendingRemoteCandidates = [];
      this.isMakingOffer = false;
      this.ignoreOffer = false;
      this._lastBytesSent = 0;
      this._lastStatsTimestamp = 0;
      this._connectionStartTime = Date.now();
      this._telemetryReported = false;

      this._initPeerConnection();
      this._attachTracksToPC();
      await this.preconnect(newRoomId, newPartnerId);
      console.log(
        `[WebRTC Lifecycle] Prepared round in room ${newRoomId} with partner ${newPartnerId}`,
      );
    } catch (err) {
      console.error("[WebRTC] prepareRound failed:", err);
      this._teardownPeerConnection();
      if (this.onFailure) this.onFailure("PARTNER_SWITCH_FAILED");
      throw err;
    }
  }

  /**
   * End the current round: close peer connection but keep local media
   * capture active for the next round's preconnect.
   */
  async endRound() {
    this._stopStatsMonitor();
    this.isMuted = true;
    this._teardownPeerConnection();
    this._pendingRemoteCandidates = [];
    this.remoteStream = new MediaStream();
    this.state = TransportState.PRECONNECTED;
    console.log("[WebRTC Lifecycle] Round ended; peer connection closed, media retained.");
  }

  /**
   * Seamless round switch: teardown previous peer connection while
   * keeping the hardware media stream active and untouched.
   */
  async switchPartner(newRoomId, newPartnerId) {
    try {
      this._teardownPeerConnection();

      this.roomId = newRoomId;
      this.partnerId = String(newPartnerId);
      this.isPolite = this.myId < this.partnerId;
      this.remoteStream = new MediaStream();
      this._pendingRemoteCandidates = [];
      this.isMakingOffer = false;
      this.ignoreOffer = false;
      this._lastBytesSent = 0;
      this._lastStatsTimestamp = 0;
      this._connectionStartTime = Date.now();
      this._telemetryReported = false;

      this._initPeerConnection();
      this._attachTracksToPC();

      this.isMuted = false;
      this.state = TransportState.OPEN;
      await this._applySenderMuteState();
      await this._applyVideoBitrateCap();
      console.log(`[WebRTC Lifecycle] Switched to room ${newRoomId} with partner ${newPartnerId}`);
    } catch (err) {
      console.error("[WebRTC] Partner switch failed:", err);
      this._teardownPeerConnection();
      if (this.onFailure) this.onFailure("PARTNER_SWITCH_FAILED");
      throw err;
    }
  }

  /**
   * Full teardown: cleanly releases hardware camera/mic and terminates peer connection.
   */
  leave() {
    this._stopStatsMonitor();
    this.isMuted = true;
    this._teardownPeerConnection();
    releaseSharedLocalMedia();
    this.localStream = null;
    this.state = TransportState.CLOSED;
    if (this.onStateChange) {
      this.onStateChange("closed");
    }
    console.log("[WebRTC Lifecycle] Session destroyed, devices released.");
  }

  // --- Signaling Handlers ---

  async handleSignalingMessage(message) {
    const { type, payload } = message;
    if (!payload || payload.room_id !== this.roomId) return;

    switch (type) {
      case "server.webrtc.offer":
        await this._handleRemoteOffer(payload);
        break;
      case "server.webrtc.answer":
        await this._handleRemoteAnswer(payload);
        break;
      case "server.webrtc.ice_candidate":
        await this._handleRemoteIceCandidate(payload);
        break;
    }
  }

  async _handleRemoteOffer(payload) {
    const pc = this.pc;
    if (!pc) return;
    const offerCollision = this.isMakingOffer || pc.signalingState !== "stable";
    this.ignoreOffer = !this.isPolite && offerCollision;

    if (this.ignoreOffer) {
      console.warn("[WebRTC] Glare collision: Impolite peer ignoring remote offer.");
      return;
    }

    try {
      // Implicit rollback is atomic with the incoming offer. A separate local
      // rollback can trigger negotiationneeded between these two operations.
      // https://www.w3.org/TR/webrtc/#perfect-negotiation-example
      await pc.setRemoteDescription({ type: "offer", sdp: payload.sdp });
      if (this.pc !== pc) return;
      await this._flushRemoteCandidates(pc);
      await pc.setLocalDescription();
      if (this.pc !== pc) return;
      this.sendSignal({
        type: "client.webrtc.answer",
        version: 1,
        payload: {
          room_id: this.roomId,
          sdp: pc.localDescription.sdp,
        },
      });
      void this._applyVideoBitrateCap();
    } catch (err) {
      if (this.pc !== pc) return;
      console.error("[WebRTC] Error handling remote offer:", err);
      if (this.onFailure) this.onFailure("RENEGOTIATION_FAILED");
    }
  }

  async _handleRemoteAnswer(payload) {
    const pc = this.pc;
    if (!pc) return;
    try {
      await pc.setRemoteDescription({ type: "answer", sdp: payload.sdp });
      if (this.pc !== pc) return;
      await this._flushRemoteCandidates(pc);
      if (this.pc !== pc) return;
      await this._applyVideoBitrateCap();
    } catch (err) {
      if (this.pc !== pc) return;
      console.error("[WebRTC] Error handling remote answer:", err);
      if (this.onFailure) this.onFailure("RENEGOTIATION_FAILED");
    }
  }

  async _handleRemoteIceCandidate(payload) {
    if (!this.pc) return;
    const candidate = {
      candidate: payload.candidate,
      sdpMid: payload.sdp_mid,
      sdpMLineIndex: payload.sdp_mline_index,
    };
    if (!this.pc.remoteDescription) {
      this._pendingRemoteCandidates.push(candidate);
      return;
    }
    try {
      await this.pc.addIceCandidate(candidate);
    } catch (err) {
      if (!this.ignoreOffer) {
        console.error("[WebRTC] Failed to add ICE candidate:", err);
      }
    }
  }

  async _flushRemoteCandidates(pc = this.pc) {
    if (!pc?.remoteDescription || !this._pendingRemoteCandidates.length) return;
    const pending = this._pendingRemoteCandidates.splice(0);
    for (const candidate of pending) {
      try {
        await pc.addIceCandidate(candidate);
      } catch (err) {
        if (!this.ignoreOffer) console.warn("[WebRTC] Queued ICE candidate rejected:", err);
      }
    }
  }

  // --- Real-Time Bitrate & Stats Monitoring ---

  _startStatsMonitor() {
    this._stopStatsMonitor();
    this.statsInterval = setInterval(async () => {
      const pc = this.pc;
      const roomId = this.roomId;
      if (!pc || pc.connectionState !== "connected") return;
      try {
        const stats = await pc.getStats();
        if (this.pc !== pc || this.roomId !== roomId) return;
        let bytesSent = 0;
        let timestamp = 0;

        stats.forEach((report) => {
          if (report.type === "outbound-rtp" && report.kind === "video") {
            bytesSent = report.bytesSent;
            timestamp = report.timestamp;
          }
        });

        if (this._lastStatsTimestamp && timestamp > this._lastStatsTimestamp) {
          const deltaSeconds = (timestamp - this._lastStatsTimestamp) / 1000;
          const deltaBytes = bytesSent - this._lastBytesSent;
          const bitrateKbps = (deltaBytes * 8) / (deltaSeconds * 1000);

          if (this.onStats) {
            this.onStats({ bitrateKbps: Math.round(bitrateKbps * 10) / 10 });
          }
        }

        this._lastBytesSent = bytesSent;
        this._lastStatsTimestamp = timestamp;
        await this._reportInboundMedia(stats, pc, roomId);
      } catch {
        // Ignored during connection state transitions
      }
    }, 2000);
  }

  _stopStatsMonitor() {
    if (this.statsInterval) {
      clearInterval(this.statsInterval);
      this.statsInterval = null;
    }
  }

  _cancelMediaReceiptReport() {
    this._mediaReportAbort?.abort();
    this._mediaReportAbort = null;
    this._mediaReportInFlight = false;
  }

  async _reportInboundMedia(stats, pc = this.pc, roomId = this.roomId) {
    if (
      this._mediaReceivedReported ||
      this._mediaReportInFlight ||
      !pc ||
      !roomId ||
      this.pc !== pc ||
      this.roomId !== roomId
    ) return false;

    let audioBytesReceived = 0;
    let videoFramesReceived = 0;
    stats?.forEach?.((report) => {
      if (report.type !== "inbound-rtp") return;
      const kind = report.kind || report.mediaType;
      if (kind === "audio") {
        audioBytesReceived += Number(report.bytesReceived) || 0;
      } else if (kind === "video") {
        // framesDecoded is preferred; framesReceived supports older browsers.
        videoFramesReceived += Number(report.framesDecoded ?? report.framesReceived) || 0;
      }
    });
    if (audioBytesReceived <= 0 || videoFramesReceived <= 0) return false;

    const connection = await extractConnectionTelemetry(pc, this._connectionStartTime, stats);
    if (
      !connection ||
      this.pc !== pc ||
      this.roomId !== roomId
    ) return false;

    const controller = new AbortController();
    this._mediaReportAbort = controller;
    this._mediaReportInFlight = true;
    try {
      const response = await fetch("/api/telemetry/connection/", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          room_id: roomId,
          connection_type: connection.connectionType,
          connection_time_ms: connection.connectionTimeMs,
          audio_bytes_received: audioBytesReceived,
          video_frames_decoded: videoFramesReceived,
        }),
        signal: controller.signal,
      });
      if (
        !response?.ok ||
        controller.signal.aborted ||
        this.pc !== pc ||
        this.roomId !== roomId
      ) return false;
      this._mediaReceivedReported = true;
      return true;
    } catch {
      // The monitor retries on a later stats sample. Avoid logging room/user data.
      return false;
    } finally {
      if (this._mediaReportAbort === controller) {
        this._mediaReportAbort = null;
        this._mediaReportInFlight = false;
      }
    }
  }
}

/**
 * استخراج قطعی نوع کاندید برنده (Direct vs Relay) و زمان سپری‌شده (T-37).
 */
export async function extractConnectionTelemetry(pc, connectionStartTimeMs, statsOverride = null) {
  if (!pc || (!statsOverride && typeof pc.getStats !== "function")) return null;

  try {
    const stats = statsOverride || await pc.getStats();
    let selectedPair = null;

    stats.forEach((report) => {
      if (
        report.type === "candidate-pair" &&
        (report.selected || report.nominated || report.state === "succeeded")
      ) {
        selectedPair = report;
      }
    });

    if (!selectedPair) return null;

    const getReport = (id) => {
      if (!id) return null;
      if (typeof stats.get === "function") return stats.get(id);
      let found = null;
      stats.forEach((item) => {
        if (item.id === id) found = item;
      });
      return found;
    };

    const localCand = getReport(selectedPair.localCandidateId);
    const remoteCand = getReport(selectedPair.remoteCandidateId);

    const isRelay =
      localCand?.candidateType === "relay" || remoteCand?.candidateType === "relay";

    const connectionType = isRelay ? "relay" : "direct";
    const startTime = typeof connectionStartTimeMs === "number" ? connectionStartTimeMs : Date.now();
    const connectionTimeMs = Math.max(0, Math.round(Date.now() - startTime));

    return { connectionType, connectionTimeMs };
  } catch {
    return null;
  }
}

/**
 * ارسال گزارش آمار اتصال به سرور بدون نشت اطلاعات یا توکن (T-37).
 */
export async function reportConnectionTelemetry(pc, roomId, connectionStartTimeMs) {
  if (!pc || !roomId) return;

  try {
    const telemetry = await extractConnectionTelemetry(pc, connectionStartTimeMs);
    if (!telemetry) return;

    const payload = {
      room_id: roomId,
      connection_type: telemetry.connectionType,
      connection_time_ms: telemetry.connectionTimeMs,
    };

    await fetch("/api/telemetry/connection/", {
      method: "POST",
      credentials: "same-origin",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify(payload),
    });
  } catch (err) {
    console.warn("[Telemetry] Failed to extract or submit connection stats:", err);
  }
}
