/**
 * T-32: brief ICE/network drops recover on the same peer connection
 * without ending the round, re-requesting devices, or resetting the timer.
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
    this.restartIceCalls = 0;
    this.lastOfferOptions = null;
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
  restartIce() {
    this.restartIceCalls += 1;
    queueMicrotask(() => this.onnegotiationneeded?.());
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
  async createOffer(options) {
    this.lastOfferOptions = options || null;
    return {
      type: "offer",
      sdp: options?.iceRestart ? "fake-offer-ice-restart" : "fake-offer",
    };
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
const negotiatorHref = pathToFileURL(
  path.join(repoRoot, "apps/events/static/events/js/perfect_negotiator.js"),
).href;
const callRoomHref = pathToFileURL(
  path.join(repoRoot, "apps/events/static/events/js/call_room.js"),
).href;

const negotiatorMod = await import(negotiatorHref);
const callRoomMod = await import(callRoomHref);

const {
  PerfectNegotiator,
  resetSharedLocalMediaForTests,
  getSharedGetUserMediaCallCount,
  ConnectionQuality,
} = negotiatorMod;
const { CallRoomController, CallRoomPhase, computeRemainingMs } = callRoomMod;

const GRACE_MS = 40;
const RESTART_DELAY_MS = 15;

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function makeNegotiator(overrides = {}) {
  return new PerfectNegotiator({
    myParticipantId: "participant-a",
    partnerParticipantId: "participant-b",
    roomId: "room-1",
    sendSignalingMessage: () => {},
    transientIceGraceMs: GRACE_MS,
    iceRestartAfterDisconnectMs: RESTART_DELAY_MS,
    ...overrides,
  });
}

function pairingPayload(roundNumber, roomId, partnerId, roundEndTs) {
  return {
    round_number: roundNumber,
    room_id: roomId,
    partner_id: partnerId,
    is_offerer: true,
    round_start_ts: Date.now(),
    round_end_ts: roundEndTs ?? Date.now() + 120_000,
    partner_display_name: `Partner ${roundNumber}`,
    partner_tags: ["ai"],
  };
}

function setConn(pc, state) {
  pc.connectionState = state;
  pc.onconnectionstatechange?.();
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

test("disconnected shows degraded and does not fail immediately", async () => {
  const states = [];
  const failures = [];
  const negotiator = makeNegotiator({
    onStateChange: (state) => states.push(state),
    onFailure: (reason) => failures.push(reason),
  });
  await negotiator.acquireLocalMedia();
  const pc = negotiator.pc;

  setConn(pc, "connected");
  setConn(pc, "disconnected");

  assert.equal(negotiator.quality, ConnectionQuality.DEGRADED);
  assert.equal(states.at(-1), "degraded");
  assert.deepEqual(failures, []);
  assert.equal(pc.restartIceCalls, 0);
  negotiator.leave();
});

test("brief disconnected then connected recovers without ICE restart or failure", async () => {
  const failures = [];
  const negotiator = makeNegotiator({
    onFailure: (reason) => failures.push(reason),
  });
  await negotiator.acquireLocalMedia();
  const pc = negotiator.pc;
  const stream = negotiator.localStream;
  const addTrackCalls = pc.addTrackCalls;

  setConn(pc, "connected");
  setConn(pc, "disconnected");
  setConn(pc, "connected");
  await delay(RESTART_DELAY_MS + 20);

  assert.equal(negotiator.quality, ConnectionQuality.CONNECTED);
  assert.deepEqual(failures, []);
  assert.equal(negotiator.pc, pc);
  assert.equal(pc.closed, false);
  assert.equal(pc.restartIceCalls, 0);
  assert.equal(pc.addTrackCalls, addTrackCalls);
  assert.equal(negotiator.localStream, stream);
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  assert.equal(globalThis.__gumCalls, 1);
  negotiator.leave();
});

test("failed triggers ICE restart on the same PC and recovers before grace expires", async () => {
  const failures = [];
  const states = [];
  const negotiator = makeNegotiator({
    onFailure: (reason) => failures.push(reason),
    onStateChange: (state) => states.push(state),
  });
  await negotiator.acquireLocalMedia();
  const pc = negotiator.pc;
  const stream = negotiator.localStream;
  const pcCount = FakePC.instances.length;

  setConn(pc, "connected");
  setConn(pc, "failed");

  assert.equal(negotiator.quality, ConnectionQuality.DEGRADED);
  assert.equal(pc.restartIceCalls, 1);
  assert.equal(FakePC.instances.length, pcCount);
  assert.equal(getSharedGetUserMediaCallCount(), 1);

  setConn(pc, "connected");
  await delay(GRACE_MS + 20);

  assert.equal(negotiator.quality, ConnectionQuality.CONNECTED);
  assert.deepEqual(failures, []);
  assert.equal(negotiator.pc, pc);
  assert.equal(negotiator.localStream, stream);
  assert.ok(stream.getTracks().every((track) => track.readyState === "live"));
  assert.ok(states.includes("degraded"));
  assert.equal(states.at(-1), "connected");
  negotiator.leave();
});

test("duplicate failed/disconnected events request ICE restart only once", async () => {
  const negotiator = makeNegotiator();
  await negotiator.acquireLocalMedia();
  const pc = negotiator.pc;

  setConn(pc, "failed");
  setConn(pc, "failed");
  setConn(pc, "disconnected");
  await delay(RESTART_DELAY_MS + 10);

  assert.equal(pc.restartIceCalls, 1);
  negotiator.leave();
});

test("disconnected schedules a single delayed ICE restart if still down", async () => {
  const negotiator = makeNegotiator();
  await negotiator.acquireLocalMedia();
  const pc = negotiator.pc;

  setConn(pc, "disconnected");
  setConn(pc, "disconnected");
  assert.equal(pc.restartIceCalls, 0);
  await delay(RESTART_DELAY_MS + 15);
  assert.equal(pc.restartIceCalls, 1);
  negotiator.leave();
});

test("stale ICE failure after teardown does not fail the new partner PC", async () => {
  const failures = [];
  const negotiator = makeNegotiator({
    onFailure: (reason) => failures.push(reason),
  });
  await negotiator.acquireLocalMedia();
  const oldPc = negotiator.pc;

  await negotiator.switchPartner("room-2", "partner-z");
  const newPc = negotiator.pc;

  oldPc.connectionState = "failed";
  oldPc.onconnectionstatechange?.();
  await delay(GRACE_MS + 20);

  assert.deepEqual(failures, []);
  assert.equal(negotiator.pc, newPc);
  assert.equal(oldPc.onnegotiationneeded, null);
  assert.equal(oldPc.onconnectionstatechange, null);
  assert.equal(oldPc.restartIceCalls, 0);
  negotiator.leave();
});

test("permanent failure after grace keeps media and does not close the transport as leave()", async () => {
  const failures = [];
  const negotiator = makeNegotiator({
    onFailure: (reason) => failures.push(reason),
  });
  await negotiator.acquireLocalMedia();
  const stream = negotiator.localStream;
  const pc = negotiator.pc;

  setConn(pc, "failed");
  await delay(GRACE_MS + 20);

  assert.deepEqual(failures, ["ICE_CONNECTION_FAILED"]);
  assert.equal(negotiator.quality, ConnectionQuality.FAILED);
  assert.equal(negotiator.localStream, stream);
  assert.ok(stream.getTracks().every((track) => track.readyState === "live"));
  assert.notEqual(negotiator.state, "closed");
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  negotiator.leave();
});

test("legacy ICE restart offer is used when restartIce is unavailable", async () => {
  const signals = [];
  const negotiator = makeNegotiator({
    sendSignalingMessage: (msg) => signals.push(msg),
  });
  await negotiator.acquireLocalMedia();
  const pc = negotiator.pc;
  pc.restartIce = undefined;

  setConn(pc, "failed");
  await delay(20);

  const restartOffer = signals.find(
    (msg) => msg.type === "client.webrtc.offer" && msg.payload?.sdp === "fake-offer-ice-restart",
  );
  assert.ok(restartOffer);
  assert.equal(pc.lastOfferOptions?.iceRestart, true);
  negotiator.leave();
});

test("call room shows quality drop then connected without resetting the timer", async () => {
  const connectionBadge = { textContent: "", className: "", dataset: {} };
  const qualityBanner = { hidden: true, textContent: "" };
  const timerDisplay = { textContent: "", dataset: {} };
  const timerContainer = { dataset: {} };
  const errorBanner = { hidden: true, textContent: "" };
  const localVideo = { srcObject: null };
  const ticks = [];

  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    transientIceGraceMs: GRACE_MS,
    iceRestartAfterDisconnectMs: RESTART_DELAY_MS,
    elements: {
      connectionBadge,
      qualityBanner,
      timerDisplay,
      timerContainer,
      errorBanner,
      localVideo,
      remoteVideo: { srcObject: null },
    },
    onTimerTick: (tick) => ticks.push(tick.remainingMs),
  });
  controller.clockSync.offsetMs = 0;
  controller.clockSync.status = "synchronised";

  await controller.ensureLocalMedia();
  const stream = controller._sharedStream;
  const payload = pairingPayload(1, "room-1", "partner-b");
  await controller._onPairing(payload);
  await controller._onRoundStart({ round_number: 1 });

  const roundEndTs = controller.roundEndTs;
  const remainingBefore = computeRemainingMs(roundEndTs, 0, Date.now());
  const pc = controller.negotiator.pc;
  const pcCount = FakePC.instances.length;

  setConn(pc, "connected");
  setConn(pc, "disconnected");

  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(connectionBadge.textContent, "QUALITY DROP");
  assert.equal(connectionBadge.dataset.quality, "degraded");
  assert.equal(qualityBanner.hidden, false);
  assert.match(qualityBanner.textContent, /quality drop/i);
  assert.equal(errorBanner.hidden, true);
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.equal(controller.roundNumber, 1);
  assert.equal(controller.negotiator.pc, pc);
  assert.equal(controller.negotiator.localStream, stream);
  assert.equal(getSharedGetUserMediaCallCount(), 1);

  await delay(30);
  const remainingDuring = computeRemainingMs(controller.roundEndTs, 0, Date.now());
  assert.ok(remainingDuring <= remainingBefore);
  assert.ok(remainingDuring > remainingBefore - 2000);

  setConn(pc, "connected");

  assert.equal(connectionBadge.textContent, "CONNECTED");
  assert.equal(connectionBadge.dataset.quality, "connected");
  assert.equal(qualityBanner.hidden, true);
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.equal(FakePC.instances.length, pcCount);
  assert.equal(localVideo.srcObject, stream);
  controller.disconnect();
});

test("signaling drop during a round keeps the timer and resumes without IDLE reset", async () => {
  const connectionBadge = { textContent: "", className: "", dataset: {} };
  const qualityBanner = { hidden: true, textContent: "" };
  const timerDisplay = { textContent: "", dataset: {} };
  const timerContainer = { dataset: {} };
  const localVideo = { srcObject: null };

  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    transientIceGraceMs: GRACE_MS,
    iceRestartAfterDisconnectMs: RESTART_DELAY_MS,
    elements: {
      connectionBadge,
      qualityBanner,
      timerDisplay,
      timerContainer,
      localVideo,
      remoteVideo: { srcObject: null },
    },
  });
  controller.clockSync.offsetMs = 0;
  controller.clockSync.status = "synchronised";

  await controller.ensureLocalMedia();
  await controller._onPairing(pairingPayload(2, "room-2", "partner-c"));
  await controller._onRoundStart({ round_number: 2 });
  const roundEndTs = controller.roundEndTs;
  const stream = controller.negotiator.localStream;
  setConn(controller.negotiator.pc, "connected");

  controller._onSignalingClosed();

  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.ok(controller._timerInterval);
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.equal(connectionBadge.dataset.quality, "degraded");
  assert.equal(qualityBanner.hidden, false);
  assert.equal(controller.negotiator.localStream, stream);
  assert.equal(getSharedGetUserMediaCallCount(), 1);

  controller._onServerHello({ server_ts: Date.now() });

  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.roundNumber, 2);
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.equal(getSharedGetUserMediaCallCount(), 1);

  controller.disconnect();
});

test("permanent ICE failure is isolated to the current partner and does not end the event", async () => {
  const connectionBadge = { textContent: "", className: "", dataset: {} };
  const errorBanner = { hidden: true, textContent: "" };
  const qualityBanner = { hidden: true, textContent: "" };
  const localVideo = { srcObject: null };
  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    transientIceGraceMs: GRACE_MS,
    iceRestartAfterDisconnectMs: RESTART_DELAY_MS,
    elements: {
      connectionBadge,
      errorBanner,
      qualityBanner,
      localVideo,
      remoteVideo: { srcObject: null },
    },
  });
  controller.clockSync.offsetMs = 0;
  controller.clockSync.status = "synchronised";

  await controller.ensureLocalMedia();
  const stream = controller._sharedStream;
  await controller._onPairing(pairingPayload(1, "room-1", "partner-b"));
  await controller._onRoundStart({ round_number: 1 });
  const roundEndTs = controller.roundEndTs;

  setConn(controller.negotiator.pc, "failed");
  await delay(GRACE_MS + 20);

  assert.equal(errorBanner.hidden, false);
  assert.match(errorBanner.textContent, /camera stays on/i);
  assert.match(connectionBadge.textContent, /FAILED/);
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.notEqual(controller.phase, CallRoomPhase.EVENT_ENDED);
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.equal(controller.negotiator.localStream, stream);
  assert.ok(stream.getTracks().every((track) => track.readyState === "live"));
  assert.equal(getSharedGetUserMediaCallCount(), 1);

  controller.disconnect();
});
