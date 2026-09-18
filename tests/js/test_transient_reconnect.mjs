/**
 * T-33: 5–20s network loss reconnects the T-14 socket with bounded backoff
 * to the same partner/room/round without resetting the timer or media.
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

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.CONNECTING;
    this.sent = [];
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    FakeWebSocket.instances.push(this);
  }
  send(data) {
    this.sent.push(typeof data === "string" ? JSON.parse(data) : data);
  }
  close() {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.();
  }
  open() {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
}
FakeWebSocket.instances = [];

function installBrowserMocks() {
  globalThis.MediaStream = FakeStream;
  globalThis.RTCPeerConnection = FakePC;
  globalThis.WebSocket = FakeWebSocket;
  globalThis.window = globalThis;
  globalThis.location = { protocol: "http:", host: "localhost" };

  const listeners = new Map();
  globalThis.addEventListener = (type, handler) => {
    const list = listeners.get(type) || [];
    list.push(handler);
    listeners.set(type, list);
  };
  globalThis.removeEventListener = (type, handler) => {
    const list = listeners.get(type) || [];
    listeners.set(
      type,
      list.filter((item) => item !== handler),
    );
  };
  globalThis.__dispatch = (type) => {
    (listeners.get(type) || []).forEach((handler) => handler());
  };
  globalThis.__listeners = listeners;

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
} = negotiatorMod;
const {
  CallRoomController,
  CallRoomPhase,
  computeRemainingMs,
  nextReconnectDelayMs,
  reconnectWindowExhausted,
  RECONNECT_INITIAL_DELAY_MS,
  RECONNECT_MAX_DELAY_MS,
  RECONNECT_WINDOW_MS,
} = callRoomMod;

function delay(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function uiElements() {
  return {
    connectionBadge: { textContent: "", className: "", dataset: {} },
    qualityBanner: { hidden: true, textContent: "" },
    timerDisplay: { textContent: "", dataset: {} },
    timerContainer: { dataset: {} },
    errorBanner: { hidden: true, textContent: "" },
    localVideo: { srcObject: null },
    remoteVideo: { srcObject: null },
    partnerName: { textContent: "" },
    roundLabel: { textContent: "" },
    roomLabel: { textContent: "" },
    phaseBadge: { textContent: "", dataset: {} },
    statusFooter: { textContent: "" },
  };
}

const liveControllers = [];

function makeController(overrides = {}) {
  const elements = overrides.elements || uiElements();
  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    reconnectInitialDelayMs: 12,
    reconnectMaxDelayMs: 40,
    reconnectWindowMs: 120,
    createWebSocket: (url) => new FakeWebSocket(url),
    elements,
    ...overrides,
  });
  liveControllers.push(controller);
  return controller;
}

async function enterRound(controller, payload = pairingPayload(1, "room-1", "partner-b")) {
  controller.clockSync.offsetMs = 0;
  controller.clockSync.status = "synchronised";
  await controller.ensureLocalMedia();
  await controller._onPairing(payload);
  await controller._onRoundStart({ round_number: payload.round_number });
  return payload;
}

before(() => {
  FakePC.instances = [];
  FakeWebSocket.instances = [];
});

beforeEach(() => {
  FakePC.instances = [];
  FakeWebSocket.instances = [];
  globalThis.__gumCalls = 0;
  globalThis.__listeners?.clear();
  resetSharedLocalMediaForTests();
});

afterEach(() => {
  for (const controller of liveControllers) {
    try {
      controller.disconnect();
    } catch {
      // ignore
    }
  }
  liveControllers.length = 0;
  resetSharedLocalMediaForTests();
});

test("bounded exponential backoff caps at max delay", () => {
  assert.equal(nextReconnectDelayMs(0, 500, 4000), 500);
  assert.equal(nextReconnectDelayMs(1, 500, 4000), 1000);
  assert.equal(nextReconnectDelayMs(2, 500, 4000), 2000);
  assert.equal(nextReconnectDelayMs(3, 500, 4000), 4000);
  assert.equal(nextReconnectDelayMs(6, 500, 4000), 4000);
  assert.equal(RECONNECT_INITIAL_DELAY_MS, 500);
  assert.equal(RECONNECT_MAX_DELAY_MS, 4000);
  assert.equal(RECONNECT_WINDOW_MS, 45_000);
  assert.equal(reconnectWindowExhausted(0, 44_999, 45_000), false);
  assert.equal(reconnectWindowExhausted(0, 45_000, 45_000), true);
});

test("signaling drop shows RECONNECTING and keeps the same partner/round/timer", async () => {
  const elements = uiElements();
  const controller = makeController({ elements });
  const payload = await enterRound(controller);
  const pc = controller.negotiator.pc;
  const stream = controller.negotiator.localStream;
  const roundEndTs = controller.roundEndTs;
  setConn(pc, "connected");

  const remainingBefore = computeRemainingMs(roundEndTs, 0, Date.now());
  controller._onSignalingClosed();

  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.roundNumber, 1);
  assert.equal(controller.partner.partnerId, "partner-b");
  assert.equal(controller.partner.roomId, "room-1");
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.ok(controller._timerInterval);
  assert.equal(elements.connectionBadge.textContent, "RECONNECTING");
  assert.equal(elements.connectionBadge.dataset.quality, "reconnecting");
  assert.match(elements.qualityBanner.textContent, /RECONNECTING/);
  assert.equal(controller.negotiator.pc, pc);
  assert.equal(controller.negotiator.localStream, stream);
  assert.equal(getSharedGetUserMediaCallCount(), 1);

  await delay(30);
  const remainingDuring = computeRemainingMs(controller.roundEndTs, 0, Date.now());
  assert.ok(remainingDuring <= remainingBefore);
  assert.ok(remainingDuring > remainingBefore - 2000);
  controller.disconnect();
});

test("backoff retries T-14 hello on a new socket and ignores the stale one", async () => {
  const controller = makeController();
  controller.connect();
  assert.equal(FakeWebSocket.instances.length, 1);
  const first = FakeWebSocket.instances[0];
  first.open();
  assert.equal(first.sent[0].type, "client.hello");

  first.close();
  assert.equal(controller.phase, CallRoomPhase.DISCONNECTED);
  await delay(20);

  assert.ok(FakeWebSocket.instances.length >= 2);
  const second = FakeWebSocket.instances[1];
  assert.equal(first.onclose, null);
  assert.equal(first.onmessage, null);
  assert.notEqual(second, first);

  const generation = controller._wsGeneration;
  first.onclose?.();
  first.close();
  assert.equal(controller._wsGeneration, generation);

  second.open();
  assert.equal(second.sent[0].type, "client.hello");
  assert.equal(second.sent[0].version, 1);
  controller.disconnect();
});

test("hello after in-round drop restores the same PC/media and kicks T-32 ICE restart", async () => {
  const elements = uiElements();
  const controller = makeController({ elements });
  await enterRound(controller);
  const pc = controller.negotiator.pc;
  const stream = controller.negotiator.localStream;
  const roundEndTs = controller.roundEndTs;
  const pcCount = FakePC.instances.length;
  setConn(pc, "connected");
  setConn(pc, "failed");
  controller._onSignalingClosed();

  assert.equal(elements.connectionBadge.textContent, "RECONNECTING");
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);

  await delay(30);
  controller._onServerHello({ server_ts: Date.now() });

  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.roundNumber, 1);
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.equal(controller.partner.partnerId, "partner-b");
  assert.equal(controller.negotiator.pc, pc);
  assert.equal(controller.negotiator.localStream, stream);
  assert.equal(FakePC.instances.length, pcCount);
  assert.ok(pc.restartIceCalls >= 1);
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  assert.equal(globalThis.__gumCalls, 1);
  assert.ok(stream.getTracks().every((track) => track.readyState === "live"));
  controller.disconnect();
});

test("same pairing snapshot does not create a second PeerConnection or offer", async () => {
  const controller = makeController();
  const payload = await enterRound(controller);
  const pc = controller.negotiator.pc;
  const pcCount = FakePC.instances.length;
  setConn(pc, "connected");
  controller._onSignalingClosed();

  await controller._onPairing(payload);
  await controller._onRoundStart({ round_number: 1 });

  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.negotiator.pc, pc);
  assert.equal(FakePC.instances.length, pcCount);
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  controller.disconnect();
});

test("retry window closes the stale socket without ending the event", async () => {
  const elements = uiElements();
  const controller = makeController({
    elements,
    reconnectInitialDelayMs: 5,
    reconnectMaxDelayMs: 5,
    reconnectWindowMs: 20,
  });
  await enterRound(controller);
  const roundEndTs = controller.roundEndTs;
  controller._onSignalingClosed();
  await delay(50);

  assert.equal(controller._reconnectGaveUp, true);
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.notEqual(controller.phase, CallRoomPhase.EVENT_ENDED);
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.equal(controller.ws, null);
  assert.equal(elements.connectionBadge.textContent, "FAILED");
  assert.equal(elements.errorBanner.hidden, false);
  assert.ok(controller._timerInterval);
  controller.disconnect();
});

test("online event retries immediately after the window was exhausted", async () => {
  const controller = makeController({
    reconnectInitialDelayMs: 5,
    reconnectMaxDelayMs: 5,
    reconnectWindowMs: 15,
  });
  await enterRound(controller);
  controller.connect();
  FakeWebSocket.instances[0].open();
  controller._onSignalingClosed();
  await delay(40);
  assert.equal(controller._reconnectGaveUp, true);
  const before = FakeWebSocket.instances.length;

  globalThis.__dispatch("online");
  assert.equal(controller._reconnectGaveUp, false);
  assert.ok(FakeWebSocket.instances.length > before);
  controller.disconnect();
});

test("one participant reconnecting does not pause another room's timer", async () => {
  const roomA = makeController({ elements: uiElements() });
  const roomB = makeController({
    myParticipantId: "participant-c",
    elements: uiElements(),
  });
  await enterRound(roomA, pairingPayload(1, "room-1", "partner-b", Date.now() + 90_000));
  await enterRound(roomB, pairingPayload(1, "room-2", "partner-d", Date.now() + 90_000));

  const endA = roomA.roundEndTs;
  const endB = roomB.roundEndTs;
  const remainingB = computeRemainingMs(endB, 0, Date.now());
  roomA._onSignalingClosed();

  assert.equal(roomA.phase, CallRoomPhase.IN_ROUND);
  assert.equal(roomB.phase, CallRoomPhase.IN_ROUND);
  assert.equal(roomB.roundEndTs, endB);
  assert.equal(roomA.roundEndTs, endA);
  assert.equal(roomB.elements.connectionBadge.textContent, "");
  const remainingBAfter = computeRemainingMs(roomB.roundEndTs, 0, Date.now());
  assert.ok(remainingBAfter <= remainingB);
  assert.ok(remainingBAfter > remainingB - 2000);
  roomA.disconnect();
  roomB.disconnect();
});

test("event_end still tears down and does not reconnect", async () => {
  const controller = makeController();
  controller.connect();
  FakeWebSocket.instances[0].open();
  await enterRound(controller);
  await controller._onEventEnd({ reason: "completed" });
  const sockets = FakeWebSocket.instances.length;
  await delay(30);
  assert.equal(controller.phase, CallRoomPhase.EVENT_ENDED);
  assert.equal(FakeWebSocket.instances.length, sockets);
  assert.equal(controller.ws, null);
  assert.equal(controller._reconnectTimer, null);
});

test("recoverAfterSignalingRestore reuses T-32 ICE restart without getUserMedia", async () => {
  const negotiator = new PerfectNegotiator({
    myParticipantId: "participant-a",
    partnerParticipantId: "participant-b",
    roomId: "room-1",
    sendSignalingMessage: () => {},
  });
  await negotiator.acquireLocalMedia();
  const pc = negotiator.pc;
  const stream = negotiator.localStream;
  setConn(pc, "failed");
  negotiator.recoverAfterSignalingRestore();
  assert.equal(negotiator.pc, pc);
  assert.ok(pc.restartIceCalls >= 1);
  assert.equal(negotiator.localStream, stream);
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  negotiator.leave();
});
