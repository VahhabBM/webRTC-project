/**
 * WebRTC Perfect Negotiation & Media Transport Lifecycle (T-27)
 * Enforces:
 *  - 640x360 target resolution
 *  - 500 kbps video bitrate cap via RTCRtpSender.setParameters
 *  - Full lifecycle transitions (idle -> preconnected -> open -> closed)
 *  - Seamless round switching (switchPartner) reusing hardware tracks
 *  - Deterministic W3C Perfect Negotiation with rollback
 *  - Hardware lock detection & simulated canvas fallback for multi-tab testing
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
  }) {
    this.myId = String(myParticipantId);
    this.partnerId = String(partnerParticipantId);
    this.roomId = roomId;
    this.sendSignal = sendSignalingMessage;
    this.rtcConfig = rtcConfig;

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

    this.localStream = null;
    this.remoteStream = new MediaStream();
    this.statsInterval = null;
    this._lastBytesSent = 0;
    this._lastStatsTimestamp = 0;

    this._initPeerConnection();
  }

  _initPeerConnection() {
    this.pc = new RTCPeerConnection(this.rtcConfig);
    this._setupPeerEvents();
  }

  _setupPeerEvents() {
    // 1. Perfect Negotiation Offer Generation
    this.pc.onnegotiationneeded = async () => {
      try {
        this.isMakingOffer = true;
        await this.pc.setLocalDescription();
        this.sendSignal({
          type: "client.webrtc.offer",
          version: 1,
          payload: {
            room_id: this.roomId,
            sdp: this.pc.localDescription.sdp,
          },
        });
      } catch (err) {
        console.error("[WebRTC] Negotiation offer error:", err);
      } finally {
        this.isMakingOffer = false;
      }
    };

    // 2. Trickle ICE
    this.pc.onicecandidate = ({ candidate }) => {
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
    this.pc.ontrack = (event) => {
      if (event.streams && event.streams[0]) {
        this.remoteStream = event.streams[0];
      } else if (event.track) {
        this.remoteStream.addTrack(event.track);
      }
      if (this.onRemoteStream) {
        this.onRemoteStream(this.remoteStream);
      }
    };

    // 4. Connection State & Degradation Monitoring
    this.pc.onconnectionstatechange = () => {
      if (!this.pc) return;
      const connState = this.pc.connectionState;
      console.log(`[WebRTC] Connection state: ${connState}`);

      if (connState === "connected") {
        this._startStatsMonitor();
        if (this.onStateChange) this.onStateChange("connected");
      } else if (connState === "disconnected") {
        if (this.onStateChange) this.onStateChange("degraded");
      } else if (connState === "failed") {
        this._stopStatsMonitor();
        if (this.onFailure) this.onFailure("ICE_CONNECTION_FAILED");
      } else if (connState === "closed") {
        this._stopStatsMonitor();
        if (this.onStateChange) this.onStateChange("closed");
      }
    };
  }

  /**
   * Acquire local hardware media. Fallback to synthetic canvas
   * if physical webcam is exclusively locked by another process/tab.
   */
  async acquireLocalMedia(constraints = DEFAULT_MEDIA_CONSTRAINTS) {
    if (!this.localStream) {
      try {
        this.localStream = await navigator.mediaDevices.getUserMedia(constraints);
        const videoTrack = this.localStream.getVideoTracks()[0];
        const settings = videoTrack?.getSettings() || {};
        if (settings.width <= 2 && settings.height <= 2) {
          throw new Error("Webcam exclusive lock detected (2x2 frame)");
        }
      } catch (err) {
        console.warn("[WebRTC] Hardware camera locked/unavailable. Using test stream:", err);
        this.localStream = this._createSimulatedStream();
      }
    }
    this._attachTracksToPC();
  }

  _createSimulatedStream() {
    const canvas = document.createElement("canvas");
    canvas.width = 640;
    canvas.height = 360;
    const ctx = canvas.getContext("2d");
    let angle = 0;

    setInterval(() => {
      ctx.fillStyle = "#1e293b";
      ctx.fillRect(0, 0, 640, 360);
      ctx.fillStyle = "#38bdf8";
      ctx.beginPath();
      ctx.arc(320 + Math.cos(angle) * 160, 180 + Math.sin(angle) * 80, 26, 0, Math.PI * 2);
      ctx.fill();
      ctx.fillStyle = "#ffffff";
      ctx.font = "20px monospace";
      ctx.fillText(`ID: ${this.myId.slice(0, 8)} | Polite: ${this.isPolite}`, 20, 40);
      angle += 0.05;
    }, 1000 / 30);

    const stream = canvas.captureStream(30);

    try {
      const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      const osc = audioCtx.createOscillator();
      const dst = audioCtx.createMediaStreamDestination();
      osc.connect(dst);
      osc.start();
      dst.stream.getAudioTracks().forEach((t) => stream.addTrack(t));
    } catch {
      // AudioContext fallback ignored
    }

    return stream;
  }

  _attachTracksToPC() {
    if (!this.localStream || !this.pc) return;
    this.localStream.getTracks().forEach((track) => {
      this.pc.addTrack(track, this.localStream);
    });
  }

  /**
   * Pre-connection phase: attaches tracks muted in background before round start.
   */
  async preconnect(roomId, partnerParticipantId) {
    this.roomId = roomId;
    this.partnerId = String(partnerParticipantId);
    this.isPolite = this.myId < this.partnerId;

    this.setMediaMuted(true);
    this.state = TransportState.PRECONNECTED;
  }

  /**
   * Open / Start Round: un-mutes tracks and enforces the 500 kbps cap.
   */
  async open() {
    this.setMediaMuted(false);
    this.state = TransportState.OPEN;
    await this._applyVideoBitrateCap();
  }

  /**
   * Mutes or un-mutes tracks without stopping hardware capture devices.
   */
  setMediaMuted(isMuted) {
    this.isMuted = isMuted;
    if (!this.localStream) return;
    this.localStream.getAudioTracks().forEach((t) => (t.enabled = !isMuted));
    this.localStream.getVideoTracks().forEach((t) => (t.enabled = !isMuted));
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

    try {
      const params = videoSender.getParameters();
      if (!params.encodings || params.encodings.length === 0) {
        params.encodings = [{}];
      }
      params.encodings[0].maxBitrate = VIDEO_MAX_BITRATE_BPS;
      await videoSender.setParameters(params);
      console.log(`[WebRTC] Video bitrate cap enforced at ${VIDEO_MAX_BITRATE_BPS / 1000} kbps`);
    } catch (err) {
      console.warn("[WebRTC] Failed to set video maxBitrate:", err);
    }
  }

  /**
   * Manual offer generation triggered by UI button or round transitions.
   */
  async createManualOffer() {
    if (!this.pc) return;
    try {
      this.isMakingOffer = true;
      const offer = await this.pc.createOffer({ offerToReceiveVideo: true, offerToReceiveAudio: true });
      await this.pc.setLocalDescription(offer);
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
      this.isMakingOffer = false;
    }
  }

  /**
   * Prepare for a new round during preconnect: teardown previous peer
   * connection, re-init with new partner, but stay muted until open().
   */
  async prepareRound(newRoomId, newPartnerId) {
    this._stopStatsMonitor();

    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }

    this.roomId = newRoomId;
    this.partnerId = String(newPartnerId);
    this.isPolite = this.myId < this.partnerId;
    this.remoteStream = new MediaStream();

    this._initPeerConnection();
    this._attachTracksToPC();
    await this.preconnect(newRoomId, newPartnerId);
    console.log(
      `[WebRTC Lifecycle] Prepared round in room ${newRoomId} with partner ${newPartnerId}`,
    );
  }

  /**
   * End the current round: close peer connection but keep local media
   * capture active for the next round's preconnect.
   */
  async endRound() {
    this._stopStatsMonitor();
    this.setMediaMuted(true);

    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }

    this.remoteStream = new MediaStream();
    this.state = TransportState.PRECONNECTED;
    console.log("[WebRTC Lifecycle] Round ended; peer connection closed, media retained.");
  }

  /**
   * Seamless round switch: teardown previous peer connection while
   * keeping the hardware media stream active and untouched.
   */
  async switchPartner(newRoomId, newPartnerId) {
    this._stopStatsMonitor();

    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }

    this.roomId = newRoomId;
    this.partnerId = String(newPartnerId);
    this.isPolite = this.myId < this.partnerId;
    this.remoteStream = new MediaStream();

    this._initPeerConnection();
    this._attachTracksToPC();

    this.state = TransportState.OPEN;
    this.setMediaMuted(false);
    await this._applyVideoBitrateCap();
    console.log(`[WebRTC Lifecycle] Switched to room ${newRoomId} with partner ${newPartnerId}`);
  }

  /**
   * Full teardown: cleanly releases hardware camera/mic and terminates peer connection.
   */
  leave() {
    this._stopStatsMonitor();
    this.setMediaMuted(true);

    if (this.localStream) {
      this.localStream.getTracks().forEach((track) => track.stop());
      this.localStream = null;
    }

    if (this.pc) {
      this.pc.close();
      this.pc = null;
    }

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
    if (!this.pc) return;
    const offerCollision = this.isMakingOffer || this.pc.signalingState !== "stable";
    this.ignoreOffer = !this.isPolite && offerCollision;

    if (this.ignoreOffer) {
      console.warn("[WebRTC] Glare collision: Impolite peer ignoring remote offer.");
      return;
    }

    try {
      if (offerCollision) {
        await this.pc.setLocalDescription({ type: "rollback" });
      }
      await this.pc.setRemoteDescription({ type: "offer", sdp: payload.sdp });
      await this.pc.setLocalDescription();
      await this._applyVideoBitrateCap();

      this.sendSignal({
        type: "client.webrtc.answer",
        version: 1,
        payload: {
          room_id: this.roomId,
          sdp: this.pc.localDescription.sdp,
        },
      });
    } catch (err) {
      console.error("[WebRTC] Error handling remote offer:", err);
    }
  }

  async _handleRemoteAnswer(payload) {
    if (!this.pc) return;
    try {
      await this.pc.setRemoteDescription({ type: "answer", sdp: payload.sdp });
      await this._applyVideoBitrateCap();
    } catch (err) {
      console.error("[WebRTC] Error handling remote answer:", err);
    }
  }

  async _handleRemoteIceCandidate(payload) {
    if (!this.pc) return;
    try {
      await this.pc.addIceCandidate({
        candidate: payload.candidate,
        sdpMid: payload.sdp_mid,
        sdpMLineIndex: payload.sdp_mline_index,
      });
    } catch (err) {
      if (!this.ignoreOffer) {
        console.error("[WebRTC] Failed to add ICE candidate:", err);
      }
    }
  }

  // --- Real-Time Bitrate & Stats Monitoring ---

  _startStatsMonitor() {
    this._stopStatsMonitor();
    this.statsInterval = setInterval(async () => {
      if (!this.pc || this.pc.connectionState !== "connected") return;
      try {
        const stats = await this.pc.getStats();
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
}