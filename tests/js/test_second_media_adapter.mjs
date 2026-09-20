/**
 * T-38: second adapter behind the T-26 media interface.
 * Pair-scoped fallback, same MediaStream/tracks, no second getUserMedia.
 */
import assert from "node:assert/strict";
import path from "node:path";
import { afterEach, before, beforeEach, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

class FakeTrack {
  constructor(kind) {
    this.kind = kind;
    this.id = `${kind}-${Math.random().toString(36).slice(2, 8)}`;
    this.enabled = true;
    this.readyState = "live";
  }
  stop() {
    this.readyState = "ended";
  }
  getSettings() {
    return { width: 640, height: 360 };
  }
}

class FakeStream {
  constructor(tracks = []) {
    this._tracks = [...tracks];
    this.id = `stream-${Math.random().toString(36).slice(2, 8)}`;
  }
  getTracks() {
    return [...this._tracks];
  }
  getAudioTracks() {
    return this._tracks.filter((track) => track.kind === "audio");
  }
  getVideoTracks() {
    return this._tracks.filter((track) => track.kind === "video");
  }
  addTrack(track) {
    this._tracks.push(track);
  }
}

class FakePC {
  constructor(config = {}) {
    this.config = config;
    this.signalingState = "stable";
    this.connectionState = "new";
    this._senders = [];
    this.onnegotiationneeded = null;
    this.onicecandidate = null;
    this.ontrack = null;
    this.onconnectionstatechange = null;
    this.localDescription = null;
    this.remoteDescription = null;
    this.addTrackCalls = 0;
    this.closed = false;
    FakePC.instances.push(this);
  }
  addTrack(track, stream) {
    this.addTrackCalls += 1;
    const sender = {
      track,
      stream,
      _trackKind: track.kind,
      replaceTrack: async (next) => {
        sender.track = next;
      },
      getParameters: () => ({ encodings: [{}] }),
      setParameters: async () => {},
    };
    this._senders.push(sender);
    queueMicrotask(() => this.onnegotiationneeded?.());
    return sender;
  }
  getSenders() {
    return this._senders;
  }
  removeTrack(sender) {
    this._senders = this._senders.filter((item) => item !== sender);
  }
  close() {
    this.closed = true;
    this.connectionState = "closed";
    this.signalingState = "closed";
    this.onconnectionstatechange?.();
  }
  async setLocalDescription(desc) {
    this.localDescription = desc || { type: "offer", sdp: "fake-sdp" };
    if (!desc || desc.type === "offer") this.signalingState = "have-local-offer";
    if (desc?.type === "answer" || desc?.type === "rollback") {
      this.signalingState = "stable";
    }
  }
  async createOffer() {
    return { type: "offer", sdp: "fake-offer" };
  }
  async setRemoteDescription(desc) {
    this.remoteDescription = desc;
    this.signalingState = desc.type === "offer" ? "have-remote-offer" : "stable";
  }
  async addIceCandidate() {}
  async getStats() {
    return new Map();
  }
  getTransceivers() {
    return [];
  }
}
FakePC.instances = [];

function installBrowserMocks() {
  globalThis.MediaStream = FakeStream;
  globalThis.RTCPeerConnection = FakePC;
  globalThis.window = globalThis;

  const getUserMedia = async () => {
    globalThis.__gumCalls = (globalThis.__gumCalls || 0) + 1;
    return new FakeStream([new FakeTrack("audio"), new FakeTrack("video")]);
  };

  const nav = globalThis.navigator;
  if (nav?.mediaDevices) {
    nav.mediaDevices.getUserMedia = getUserMedia;
  } else if (nav) {
    Object.defineProperty(nav, "mediaDevices", {
      configurable: true,
      value: { getUserMedia },
    });
  } else {
    Object.defineProperty(globalThis, "navigator", {
      configurable: true,
      value: { mediaDevices: { getUserMedia } },
    });
  }
}

installBrowserMocks();

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const mediaHref = pathToFileURL(
  path.join(repoRoot, "apps/events/static/events/js/media_transport.js"),
).href;
const callRoomHref = pathToFileURL(
  path.join(repoRoot, "apps/events/static/events/js/call_room.js"),
).href;
const negotiatorHref = pathToFileURL(
  path.join(repoRoot, "apps/events/static/events/js/perfect_negotiator.js"),
).href;

const mediaMod = await import(mediaHref);
const callRoomMod = await import(callRoomHref);
const negotiatorMod = await import(negotiatorHref);

const {
  FailoverMediaTransport,
  fallbackAllowedForRoom,
  AdapterKind,
} = mediaMod;
const { CallRoomController, CallRoomPhase } = callRoomMod;
const { resetSharedLocalMediaForTests, getSharedGetUserMediaCallCount } = negotiatorMod;

function pairingPayload(roundNumber, roomId, partnerId) {
  return {
    round_number: roundNumber,
    room_id: roomId,
    partner_id: partnerId,
    is_offerer: true,
    round_start_ts: 1_700_000_000_000,
    round_end_ts: 1_700_000_300_000,
    partner_display_name: `Partner ${roundNumber}`,
    partner_tags: ["ai"],
  };
}

before(() => {
  FakePC.instances = [];
});

beforeEach(() => {
  FakePC.instances = [];
  globalThis.__gumCalls = 0;
  resetSharedLocalMediaForTests();
});

afterEach(() => {
  resetSharedLocalMediaForTests();
});

test("fallbackAllowedForRoom is pair-scoped and off by default", () => {
  assert.equal(fallbackAllowedForRoom("test-room-101", "test-room-101"), true);
  assert.equal(fallbackAllowedForRoom("test-room-202", "test-room-101"), false);
  assert.equal(fallbackAllowedForRoom("test-room-101", ""), false);
});

test("primary path never activates fallback without the pair key", async () => {
  const failures = [];
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "room-1",
    sendSignalingMessage: () => {},
    selectedRoomId: "",
    onFailure: (reason) => failures.push(reason),
  });
  await transport.acquireLocalMedia();
  await transport.preconnect("room-1", "partner-b");
  await transport.open();
  assert.equal(transport.adapterKind, AdapterKind.DIRECT);
  assert.equal(await transport.forceFallback(), false);
  await transport._onActiveFailure(
    "ICE_CONNECTION_FAILED",
    transport._generation,
  );
  assert.equal(transport.usingFallback, false);
  assert.deepEqual(failures, ["ICE_CONNECTION_FAILED"]);
  transport.leave();
});

test("forceFallback switches one selected pair and reuses tracks", async () => {
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "test-room-101",
    sendSignalingMessage: () => {},
    selectedRoomId: "test-room-101",
  });
  await transport.acquireLocalMedia();
  const stream = transport.localStream;
  const tracks = stream.getTracks();
  await transport.preconnect("test-room-101", "partner-b");
  await transport.open();
  const oldPc = transport.pc;

  assert.equal(await transport.forceFallback(), true);

  assert.equal(transport.usingFallback, true);
  assert.equal(transport.adapterKind, AdapterKind.RELAY);
  assert.equal(transport.pc.config.iceTransportPolicy, "relay");
  assert.equal(transport.localStream, stream);
  assert.deepEqual(transport.localStream.getTracks(), tracks);
  assert.ok(tracks.every((track) => track.readyState === "live"));
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  assert.equal(globalThis.__gumCalls, 1);
  assert.notEqual(transport.pc, oldPc);
  assert.equal(oldPc.closed, true);
  assert.equal(oldPc.onnegotiationneeded, null);
  assert.equal(oldPc.onconnectionstatechange, null);
  assert.equal(transport.roomId, "test-room-101");
  assert.equal(transport.partnerId, "partner-b");
  transport.leave();
});

test("stale primary peer cannot control the room after fallback", async () => {
  const failures = [];
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "test-room-101",
    sendSignalingMessage: () => {},
    selectedRoomId: "test-room-101",
    onFailure: (reason) => failures.push(reason),
  });
  await transport.acquireLocalMedia();
  await transport.preconnect("test-room-101", "partner-b");
  await transport.open();
  const oldPc = transport.pc;
  await transport.forceFallback();

  oldPc.connectionState = "failed";
  oldPc.onconnectionstatechange?.();
  assert.deepEqual(failures, []);
  assert.equal(transport.usingFallback, true);
  transport.leave();
});

test("call room fallback is isolated to the selected pair", async () => {
  const selected = new CallRoomController({
    myParticipantId: "participant-a",
    mediaFallbackRoomId: "room-keep",
    elements: {
      localVideo: { srcObject: null },
      remoteVideo: { srcObject: null },
      connectionBadge: { textContent: "", className: "", dataset: {} },
      qualityBanner: { hidden: true, textContent: "" },
    },
  });
  const other = new CallRoomController({
    myParticipantId: "participant-c",
    mediaFallbackRoomId: "",
    elements: {
      localVideo: { srcObject: null },
      remoteVideo: { srcObject: null },
      connectionBadge: { textContent: "", className: "", dataset: {} },
    },
  });

  await selected._onPairing(pairingPayload(1, "room-keep", "partner-b"));
  await selected._onRoundStart({ round_number: 1 });
  await other._onPairing(pairingPayload(1, "room-other", "partner-d"));
  await other._onRoundStart({ round_number: 1 });

  const selectedStream = selected.negotiator.localStream;
  const otherStream = other.negotiator.localStream;
  const selectedOldPc = selected.negotiator.pc;
  const otherOldPc = other.negotiator.pc;

  assert.equal(await selected.forceMediaFallback(), true);
  assert.equal(await other.forceMediaFallback(), false);

  assert.equal(selected.phase, CallRoomPhase.IN_ROUND);
  assert.equal(other.phase, CallRoomPhase.IN_ROUND);
  assert.equal(selected.partner.partnerId, "partner-b");
  assert.equal(selected.partner.roomId, "room-keep");
  assert.equal(other.partner.partnerId, "partner-d");
  assert.equal(selected.negotiator.adapterKind, AdapterKind.RELAY);
  assert.equal(other.negotiator.adapterKind, AdapterKind.DIRECT);
  assert.equal(selected.negotiator.localStream, selectedStream);
  assert.equal(other.negotiator.localStream, otherStream);
  assert.equal(selectedOldPc.closed, true);
  assert.equal(otherOldPc.closed, false);
  assert.equal(getSharedGetUserMediaCallCount(), 1);

  selected._stopTimer();
  other._stopTimer();
  selected.disconnect();
  other.disconnect();
});

test("ICE failure on a selected pair swaps adapters without a second getUserMedia", async () => {
  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    mediaFallbackRoomId: "room-1",
    elements: {
      localVideo: { srcObject: null },
      remoteVideo: { srcObject: null },
      connectionBadge: { textContent: "", className: "", dataset: {} },
      qualityBanner: { hidden: true, textContent: "" },
      errorBanner: { hidden: true, textContent: "" },
    },
  });
  await controller._onPairing(pairingPayload(1, "room-1", "partner-b"));
  await controller._onRoundStart({ round_number: 1 });
  const stream = controller.negotiator.localStream;
  const oldPc = controller.negotiator.pc;

  await controller.negotiator._onActiveFailure(
    "ICE_CONNECTION_FAILED",
    controller.negotiator._generation,
  );

  assert.equal(controller.negotiator.usingFallback, true);
  assert.equal(controller.negotiator.localStream, stream);
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  assert.equal(oldPc.closed, true);
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.partner.roundNumber, 1);
  assert.equal(controller.elements.errorBanner.hidden, true);
  controller._stopTimer();
  controller.disconnect();
});

test("primary call-room path still works without the fallback flag", async () => {
  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    elements: {
      localVideo: { srcObject: null },
      remoteVideo: { srcObject: null },
      errorBanner: { hidden: true, textContent: "" },
    },
  });
  await controller.ensureLocalMedia();
  await controller._onPairing(pairingPayload(1, "room-1", "partner-b"));
  await controller._onRoundStart({ round_number: 1 });
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.negotiator.adapterKind, AdapterKind.DIRECT);
  assert.equal(controller.negotiator.usingFallback, false);
  assert.equal(await controller.forceMediaFallback(), false);
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  await controller._onEventEnd({ reason: "completed" });
  assert.equal(controller.phase, CallRoomPhase.EVENT_ENDED);
  assert.equal(controller.negotiator, null);
});
