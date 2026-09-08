
class PerfectNegotiator {
  constructor({ myParticipantId, partnerParticipantId, roomId, sendSignalingMessage, rtcConfig = {} }) {
    this.myId = myParticipantId;
    this.partnerId = partnerParticipantId;
    this.roomId = roomId;
    this.sendSignal = sendSignalingMessage;

    this.isPolite = this.myId < this.partnerId;

    this.isMakingOffer = false;
    this.ignoreOffer = false;

    this.pc = new RTCPeerConnection(rtcConfig);
    this._setupPeerConnection();
  }

  _setupPeerConnection() {
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
  }

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
    const offerCollision = this.isMakingOffer || this.pc.signalingState !== "stable";
    this.ignoreOffer = !this.isPolite && offerCollision;

    if (this.ignoreOffer) {
      console.warn(`[WebRTC] Peer is impolite; collided offer ignored.`);
      return;
    }

    try {
      await this.pc.setRemoteDescription({ type: "offer", sdp: payload.sdp });
      await this.pc.setLocalDescription(); 

      this.sendSignal({
        type: "client.webrtc.answer",
        version: 1,
        payload: {
          room_id: this.roomId,
          sdp: this.pc.localDescription.sdp,
        },
      });
    } catch (err) {
      console.error("[WebRTC] Error processing remote offer:", err);
    }
  }

  async _handleRemoteAnswer(payload) {
    try {
      await this.pc.setRemoteDescription({ type: "answer", sdp: payload.sdp });
    } catch (err) {
      console.error("[WebRTC] Error processing remote answer:", err);
    }
  }

  async _handleRemoteIceCandidate(payload) {
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
}