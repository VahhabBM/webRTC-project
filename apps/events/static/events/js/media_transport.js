/**
 * T-26 / T-38 / T-39 media transport surface used by the call room.
 *
 * Call-room code depends on this module only. The second (relay) adapter is
 * created behind FailoverMediaTransport and is never imported by call_room.js.
 * T-39 arms a pair-scoped escalation timer on the same FailoverMediaTransport.
 */

import {
  PerfectNegotiator,
  DEFAULT_MEDIA_CONSTRAINTS,
  acquireSharedLocalMedia,
  TransportState,
} from "./perfect_negotiator.js";

export { DEFAULT_MEDIA_CONSTRAINTS, acquireSharedLocalMedia, TransportState };

export const AdapterKind = Object.freeze({
  DIRECT: "direct",
  RELAY: "relay",
});

/** Single T-39 default. Call sites must use this name, not a raw millisecond literal. */
export const DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS = 6000;

export function fallbackAllowedForRoom(roomId, selectedRoomId) {
  if (!selectedRoomId || !roomId) return false;
  return String(roomId) === String(selectedRoomId);
}

function defaultScheduleEscalation(delayMs, callback) {
  const id = setTimeout(callback, delayMs);
  return () => clearTimeout(id);
}

export function createPrimaryMediaAdapter(options) {
  const rtcConfig = { ...(options.rtcConfig || {}) };
  delete rtcConfig.iceTransportPolicy;
  const adapter = new PerfectNegotiator({ ...options, rtcConfig });
  adapter.adapterKind = AdapterKind.DIRECT;
  return adapter;
}

function createRelayMediaAdapter(options) {
  const rtcConfig = {
    ...(options.rtcConfig || {}),
    iceTransportPolicy: "relay",
  };
  const adapter = new PerfectNegotiator({ ...options, rtcConfig });
  adapter.adapterKind = AdapterKind.RELAY;
  return adapter;
}

export class FailoverMediaTransport {
  constructor({
    createPrimary = createPrimaryMediaAdapter,
    createFallback = createRelayMediaAdapter,
    selectedRoomId = "",
    forceFallback = false,
    escalationTimeoutMs = DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS,
    scheduleEscalation = null,
    ...adapterOptions
  } = {}) {
    this._createPrimary = createPrimary;
    this._createFallback = createFallback;
    this._selectedRoomId = selectedRoomId ? String(selectedRoomId) : "";
    this._forceFallbackOnOpen = Boolean(forceFallback);
    this._escalationTimeoutMs = Number(escalationTimeoutMs);
    if (!Number.isFinite(this._escalationTimeoutMs)) {
      this._escalationTimeoutMs = DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS;
    }
    this._scheduleEscalation = scheduleEscalation || defaultScheduleEscalation;
    this._adapterOptions = adapterOptions;
    this._usingFallback = false;
    this._switching = false;
    this._generation = 0;
    this._userOnFailure = adapterOptions.onFailure || null;
    this._userOnStateChange = adapterOptions.onStateChange || null;
    this.fallbackActivations = 0;
    this._cancelEscalation = null;
    this._escalationGeneration = 0;
    this._primaryConnected = false;
    this.lastEscalationLog = null;
    this._active = this._spawn(this._createPrimary);
  }

  get pc() {
    return this._active?.pc ?? null;
  }

  get localStream() {
    return this._active?.localStream ?? null;
  }

  get roomId() {
    return this._active?.roomId;
  }

  get partnerId() {
    return this._active?.partnerId;
  }

  get myId() {
    return this._active?.myId;
  }

  get state() {
    return this._active?.state;
  }

  get quality() {
    return this._active?.quality;
  }

  get adapterKind() {
    if (this._active?.adapterKind) return this._active.adapterKind;
    return this._usingFallback ? AdapterKind.RELAY : AdapterKind.DIRECT;
  }

  get usingFallback() {
    return this._usingFallback;
  }

  get escalationTimeoutMs() {
    return this._escalationTimeoutMs;
  }

  _isPrimaryConnected() {
    if (this._usingFallback) return false;
    if (this._primaryConnected) return true;
    const conn = this.pc?.connectionState;
    const quality = this.quality;
    return conn === "connected" || quality === "connected";
  }

  _clearEscalationTimer() {
    this._escalationGeneration += 1;
    const cancel = this._cancelEscalation;
    this._cancelEscalation = null;
    if (typeof cancel === "function") {
      cancel();
    }
  }

  _armEscalationTimer() {
    this._clearEscalationTimer();
    if (this._usingFallback || this._switching) {
      return;
    }
    if (this._isPrimaryConnected()) {
      this._primaryConnected = true;
      return;
    }
    const gen = this._escalationGeneration;
    this._cancelEscalation = this._scheduleEscalation(
      this._escalationTimeoutMs,
      () => this._onEscalationTimeout(gen),
    );
  }

  _escalationLogFields(result, reason = "") {
    const roomId = this.roomId;
    return {
      room_id: roomId ?? null,
      partner_id: this.partnerId ?? null,
      primary_path: AdapterKind.DIRECT,
      timeout_ms: this._escalationTimeoutMs,
      fallback_configured: Boolean(this._selectedRoomId),
      fallback_selected_room_id: this._selectedRoomId || "",
      fallback_allowed: fallbackAllowedForRoom(roomId, this._selectedRoomId),
      using_fallback: this._usingFallback,
      result,
      reason,
    };
  }

  _recordEscalation(result, reason = "") {
    const fields = this._escalationLogFields(result, reason);
    this.lastEscalationLog = fields;
    console.info("[T-39] media_escalation", fields);
  }

  async _onEscalationTimeout(gen) {
    if (gen !== this._escalationGeneration) return;
    if (this._usingFallback || this._switching || this._primaryConnected) return;
    try {
      await this._handleEscalationTimeout();
    } catch (err) {
      this._recordEscalation("error", String(err?.message || err || "unknown"));
    }
  }

  async _handleEscalationTimeout() {
    const allowed = fallbackAllowedForRoom(this.roomId, this._selectedRoomId);
    if (!allowed) {
      this._recordEscalation("skipped", "FALLBACK_UNAVAILABLE");
      this._userOnStateChange?.("failed");
      this._userOnFailure?.("ESCALATION_FALLBACK_UNAVAILABLE");
      return;
    }
    const ok = await this._activateFallback("ESCALATION_TIMEOUT");
    this._recordEscalation(
      ok ? "activated" : "failed",
      ok ? "ESCALATION_TIMEOUT" : "FALLBACK_ACTIVATION_FAILED",
    );
    if (!ok) {
      this._userOnStateChange?.("failed");
      this._userOnFailure?.("ESCALATION_FAILED");
    }
  }

  _spawn(factory) {
    return factory(
      this._wrapOptions({
        ...this._adapterOptions,
        localStream: this._adapterOptions.localStream || this._active?.localStream || null,
        myParticipantId: this._adapterOptions.myParticipantId || this._active?.myId,
        partnerParticipantId:
          this._adapterOptions.partnerParticipantId || this._active?.partnerId,
        roomId: this._adapterOptions.roomId || this._active?.roomId,
      }),
    );
  }

  _wrapOptions(options) {
    const gen = this._generation;
    return {
      ...options,
      onFailure: (reason) => {
        void this._onActiveFailure(reason, gen);
      },
      onStateChange: (state) => {
        if (gen !== this._generation) return;
        if (state === "connected") {
          this._primaryConnected = !this._usingFallback;
          this._clearEscalationTimer();
        }
        this._userOnStateChange?.(state);
      },
    };
  }

  _snapshot() {
    return {
      stream: this._active?.localStream || this._adapterOptions.localStream || null,
      roomId: this._active?.roomId,
      partnerId: this._active?.partnerId,
      myId: this._active?.myId,
      prevState: this._active?.state,
      rtcConfig: this._adapterOptions.rtcConfig || {},
    };
  }

  _detachActive() {
    const old = this._active;
    if (!old) return;
    if (typeof old.detachPeerKeepMedia === "function") {
      old.detachPeerKeepMedia();
    } else if (typeof old._teardownPeerConnection === "function") {
      old._teardownPeerConnection();
    }
  }

  async _activateFallback(reason = "PRIMARY_FAILED") {
    if (this._switching || this._usingFallback) return false;
    const roomId = this.roomId;
    if (!fallbackAllowedForRoom(roomId, this._selectedRoomId)) return false;

    this._clearEscalationTimer();
    this._switching = true;
    this._generation += 1;
    const snap = this._snapshot();
    this._detachActive();
    this._usingFallback = true;
    this._primaryConnected = false;
    this.fallbackActivations += 1;
    this._adapterOptions = {
      ...this._adapterOptions,
      localStream: snap.stream,
      rtcConfig: snap.rtcConfig,
      myParticipantId: snap.myId,
      partnerParticipantId: snap.partnerId,
      roomId: snap.roomId,
    };
    this._active = this._spawn(this._createFallback);
    try {
      if (typeof this._active.acquireLocalMedia === "function") {
        await this._active.acquireLocalMedia(DEFAULT_MEDIA_CONSTRAINTS);
      }
      if (snap.roomId && snap.partnerId && typeof this._active.preconnect === "function") {
        await this._active.preconnect(snap.roomId, snap.partnerId);
      }
      if (
        (snap.prevState === TransportState.OPEN || snap.prevState === "open") &&
        typeof this._active.open === "function"
      ) {
        await this._active.open();
      }
    } catch (err) {
      this._switching = false;
      this._userOnFailure?.(reason || String(err));
      return false;
    }
    this._switching = false;
    this._userOnStateChange?.(this._active.quality || "connected");
    return true;
  }

  async _restorePrimary(newRoomId, newPartnerId, { open = false } = {}) {
    this._clearEscalationTimer();
    this._generation += 1;
    const snap = this._snapshot();
    this._detachActive();
    this._usingFallback = false;
    this._primaryConnected = false;
    this._adapterOptions = {
      ...this._adapterOptions,
      localStream: snap.stream,
      roomId: newRoomId,
      partnerParticipantId: newPartnerId,
    };
    this._active = this._spawn(this._createPrimary);
    if (typeof this._active.acquireLocalMedia === "function") {
      await this._active.acquireLocalMedia(DEFAULT_MEDIA_CONSTRAINTS);
    }
    if (open && typeof this._active.switchPartner === "function") {
      await this._active.switchPartner(newRoomId, newPartnerId);
      return;
    }
    if (typeof this._active.prepareRound === "function") {
      await this._active.prepareRound(newRoomId, newPartnerId);
    } else if (typeof this._active.preconnect === "function") {
      await this._active.preconnect(newRoomId, newPartnerId);
    }
  }

  async _onActiveFailure(reason, gen) {
    if (gen !== this._generation) return;
    this._clearEscalationTimer();
    const switched = await this._activateFallback(reason);
    if (!switched) {
      this._userOnFailure?.(reason);
    }
  }

  async forceFallback() {
    return this._activateFallback("TEST_TRIGGER");
  }

  async acquireLocalMedia(constraints = DEFAULT_MEDIA_CONSTRAINTS) {
    if (typeof this._active.acquireLocalMedia !== "function") {
      return this._active.localStream;
    }
    const stream = await this._active.acquireLocalMedia(constraints);
    this._adapterOptions.localStream = stream;
    return stream;
  }

  async preconnect(roomId, partnerId) {
    this._clearEscalationTimer();
    this._primaryConnected = false;
    if (
      this._usingFallback &&
      !fallbackAllowedForRoom(roomId, this._selectedRoomId)
    ) {
      await this._restorePrimary(roomId, partnerId, { open: false });
      return;
    }
    return this._active.preconnect(roomId, partnerId);
  }

  async open() {
    this._primaryConnected = false;
    await this._active.open();
    if (this._forceFallbackOnOpen) {
      await this.forceFallback();
    }
    if (!this._usingFallback) {
      this._armEscalationTimer();
    }
  }

  async createManualOffer() {
    if (typeof this._active.createManualOffer === "function") {
      return this._active.createManualOffer();
    }
  }

  async prepareRound(newRoomId, newPartnerId) {
    this._clearEscalationTimer();
    this._primaryConnected = false;
    if (
      this._usingFallback &&
      !fallbackAllowedForRoom(newRoomId, this._selectedRoomId)
    ) {
      await this._restorePrimary(newRoomId, newPartnerId, { open: false });
      return;
    }
    return this._active.prepareRound(newRoomId, newPartnerId);
  }

  async endRound() {
    this._clearEscalationTimer();
    this._primaryConnected = false;
    return this._active.endRound();
  }

  async switchPartner(newRoomId, newPartnerId) {
    this._clearEscalationTimer();
    this._primaryConnected = false;
    if (
      this._usingFallback &&
      !fallbackAllowedForRoom(newRoomId, this._selectedRoomId)
    ) {
      await this._restorePrimary(newRoomId, newPartnerId, { open: true });
      this._armEscalationTimer();
      return;
    }
    const result = await this._active.switchPartner(newRoomId, newPartnerId);
    this._armEscalationTimer();
    return result;
  }

  leave() {
    this._clearEscalationTimer();
    this._generation += 1;
    if (this._active && typeof this._active.leave === "function") {
      this._active.leave();
    }
    this._usingFallback = false;
    this._primaryConnected = false;
  }

  async handleSignalingMessage(message) {
    if (typeof this._active.handleSignalingMessage === "function") {
      return this._active.handleSignalingMessage(message);
    }
  }

  recoverAfterSignalingRestore() {
    this._clearEscalationTimer();
    let result;
    if (typeof this._active.recoverAfterSignalingRestore === "function") {
      result = this._active.recoverAfterSignalingRestore();
    }
    if (!this._usingFallback) {
      this._primaryConnected = this._isPrimaryConnected();
      if (!this._primaryConnected) {
        this._armEscalationTimer();
      }
    }
    return result;
  }

  setMediaMuted(isMuted) {
    if (typeof this._active.setMediaMuted === "function") {
      return this._active.setMediaMuted(isMuted);
    }
  }
}
