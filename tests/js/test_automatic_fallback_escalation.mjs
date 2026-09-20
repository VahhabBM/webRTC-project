/**
 * T-39: automatic escalation onto the T-38 fallback after the configured timeout.
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
  AdapterKind,
  DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS,
} = mediaMod;
const { CallRoomController, CallRoomPhase } = callRoomMod;
const { resetSharedLocalMediaForTests, getSharedGetUserMediaCallCount } = negotiatorMod;

function createManualScheduler() {
  const pending = [];
  return {
    pending,
    live() {
      return pending.filter((job) => !job.cancelled);
    },
    schedule(delayMs, cb) {
      const job = { delayMs, cb, cancelled: false };
      pending.push(job);
      return () => {
        job.cancelled = true;
      };
    },
    async firePending() {
      const jobs = pending.filter((job) => !job.cancelled);
      for (const job of jobs) {
        job.cancelled = true;
        await job.cb();
      }
    },
  };
}

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

function elements() {
  return {
    localVideo: { srcObject: null },
    remoteVideo: { srcObject: null },
    connectionBadge: { textContent: "", className: "", dataset: {} },
    qualityBanner: { hidden: true, textContent: "" },
    errorBanner: { hidden: true, textContent: "" },
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

test("default escalation timeout is the shared 6000ms constant", () => {
  assert.equal(DEFAULT_MEDIA_FALLBACK_ESCALATION_TIMEOUT_MS, 6000);
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    sendSignalingMessage: () => {},
  });
  assert.equal(transport.escalationTimeoutMs, 6000);
  transport.leave();
});

test("escalation timeout is configurable and used when arming", async () => {
  const scheduler = createManualScheduler();
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "test-room-101",
    sendSignalingMessage: () => {},
    selectedRoomId: "test-room-101",
    escalationTimeoutMs: 50,
    scheduleEscalation: (delayMs, cb) => scheduler.schedule(delayMs, cb),
  });
  await transport.acquireLocalMedia();
  await transport.preconnect("test-room-101", "partner-b");
  assert.equal(scheduler.live().length, 0);
  await transport.open();
  assert.equal(transport.escalationTimeoutMs, 50);
  assert.equal(scheduler.live().length, 1);
  assert.equal(scheduler.live()[0].delayMs, 50);
  transport.leave();
});

test("timer starts on open, cancels when primary connects, ignores stale fire", async () => {
  const scheduler = createManualScheduler();
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "test-room-101",
    sendSignalingMessage: () => {},
    selectedRoomId: "test-room-101",
    scheduleEscalation: (delayMs, cb) => scheduler.schedule(delayMs, cb),
  });
  await transport.acquireLocalMedia();
  await transport.preconnect("test-room-101", "partner-b");
  assert.equal(scheduler.live().length, 0);

  await transport.open();
  assert.equal(scheduler.live().length, 1);
  const stale = scheduler.live()[0].cb;

  transport.pc.connectionState = "connected";
  transport.pc.onconnectionstatechange?.();
  assert.equal(scheduler.live().length, 0);

  await stale();
  assert.equal(transport.usingFallback, false);
  assert.equal(transport.adapterKind, AdapterKind.DIRECT);
  assert.equal(transport.fallbackActivations, 0);
  transport.leave();
});

test("timeout activates T-38 fallback for this pair and reuses tracks", async () => {
  const scheduler = createManualScheduler();
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "test-room-101",
    sendSignalingMessage: () => {},
    selectedRoomId: "test-room-101",
    scheduleEscalation: (delayMs, cb) => scheduler.schedule(delayMs, cb),
  });
  await transport.acquireLocalMedia();
  const stream = transport.localStream;
  const tracks = stream.getTracks();
  await transport.preconnect("test-room-101", "partner-b");
  await transport.open();
  const oldPc = transport.pc;
  const pcCount = FakePC.instances.length;

  await scheduler.firePending();

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
  assert.equal(FakePC.instances.length, pcCount + 1);
  assert.equal(transport.lastEscalationLog.result, "activated");
  assert.equal(transport.lastEscalationLog.fallback_allowed, true);
  transport.leave();
});

test("no escalation when fallback is not configured; failure is visible", async () => {
  const scheduler = createManualScheduler();
  const failures = [];
  const states = [];
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "room-1",
    sendSignalingMessage: () => {},
    selectedRoomId: "",
    scheduleEscalation: (delayMs, cb) => scheduler.schedule(delayMs, cb),
    onFailure: (reason) => failures.push(reason),
    onStateChange: (state) => states.push(state),
  });
  await transport.acquireLocalMedia();
  await transport.preconnect("room-1", "partner-b");
  await transport.open();
  await scheduler.firePending();

  assert.equal(transport.usingFallback, false);
  assert.equal(transport.adapterKind, AdapterKind.DIRECT);
  assert.deepEqual(failures, ["ESCALATION_FALLBACK_UNAVAILABLE"]);
  assert.ok(states.includes("failed"));
  assert.equal(transport.lastEscalationLog.result, "skipped");
  assert.equal(transport.lastEscalationLog.reason, "FALLBACK_UNAVAILABLE");
  assert.equal(transport.lastEscalationLog.fallback_configured, false);
  const blob = JSON.stringify(transport.lastEscalationLog).toLowerCase();
  assert.equal(blob.includes("token"), false);
  assert.equal(blob.includes("secret"), false);
  transport.leave();
});

test("call-room timeout is isolated to the selected pair", async () => {
  const selectedSched = createManualScheduler();
  const otherSched = createManualScheduler();
  const selected = new CallRoomController({
    myParticipantId: "participant-a",
    mediaFallbackRoomId: "room-keep",
    scheduleEscalation: (delayMs, cb) => selectedSched.schedule(delayMs, cb),
    elements: elements(),
  });
  const other = new CallRoomController({
    myParticipantId: "participant-c",
    mediaFallbackRoomId: "",
    scheduleEscalation: (delayMs, cb) => otherSched.schedule(delayMs, cb),
    elements: elements(),
  });

  await selected._onPairing(pairingPayload(1, "room-keep", "partner-b"));
  assert.equal(selectedSched.live().length, 0);
  await selected._onRoundStart({ round_number: 1 });
  await other._onPairing(pairingPayload(1, "room-other", "partner-d"));
  await other._onRoundStart({ round_number: 1 });

  const selectedStream = selected.negotiator.localStream;
  const otherStream = other.negotiator.localStream;
  const selectedOldPc = selected.negotiator.pc;
  const otherOldPc = other.negotiator.pc;

  await selectedSched.firePending();

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
  assert.equal(other.elements.errorBanner.hidden, true);

  selected._stopTimer();
  other._stopTimer();
  selected.disconnect();
  other.disconnect();
});

test("stale timers are ignored after reconnect, round end, and teardown", async () => {
  const scheduler = createManualScheduler();
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "test-room-101",
    sendSignalingMessage: () => {},
    selectedRoomId: "test-room-101",
    scheduleEscalation: (delayMs, cb) => scheduler.schedule(delayMs, cb),
  });
  await transport.acquireLocalMedia();
  await transport.preconnect("test-room-101", "partner-b");
  await transport.open();
  const afterOpen = scheduler.live()[0].cb;

  transport.recoverAfterSignalingRestore();
  assert.equal(scheduler.live().length, 1);
  await afterOpen();
  assert.equal(transport.usingFallback, false);

  await scheduler.firePending();
  assert.equal(transport.usingFallback, true);
  assert.equal(transport.fallbackActivations, 1);
  transport.leave();

  const roundSched = createManualScheduler();
  const roundTransport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "test-room-101",
    sendSignalingMessage: () => {},
    selectedRoomId: "test-room-101",
    scheduleEscalation: (delayMs, cb) => roundSched.schedule(delayMs, cb),
  });
  await roundTransport.acquireLocalMedia();
  await roundTransport.preconnect("test-room-101", "partner-b");
  await roundTransport.open();
  const roundCb = roundSched.live()[0].cb;
  await roundTransport.endRound();
  await roundCb();
  assert.equal(roundTransport.usingFallback, false);
  roundTransport.leave();
});

test("T-38 explicit forceFallback still switches without waiting for timeout", async () => {
  const scheduler = createManualScheduler();
  const transport = new FailoverMediaTransport({
    myParticipantId: "participant-a",
    partnerParticipantId: "partner-b",
    roomId: "test-room-101",
    sendSignalingMessage: () => {},
    selectedRoomId: "test-room-101",
    forceFallback: true,
    scheduleEscalation: (delayMs, cb) => scheduler.schedule(delayMs, cb),
  });
  await transport.acquireLocalMedia();
  await transport.preconnect("test-room-101", "partner-b");
  await transport.open();

  assert.equal(transport.usingFallback, true);
  assert.equal(transport.adapterKind, AdapterKind.RELAY);
  assert.equal(scheduler.live().length, 0);
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  transport.leave();
});

test("call room shows failure when timeout fires without fallback", async () => {
  const scheduler = createManualScheduler();
  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    mediaFallbackRoomId: "",
    scheduleEscalation: (delayMs, cb) => scheduler.schedule(delayMs, cb),
    elements: elements(),
  });
  await controller._onPairing(pairingPayload(1, "room-1", "partner-b"));
  await controller._onRoundStart({ round_number: 1 });
  await scheduler.firePending();

  assert.equal(controller.negotiator.usingFallback, false);
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.elements.errorBanner.hidden, false);
  assert.match(
    controller.elements.errorBanner.textContent,
    /fallback is not available for this pair/,
  );
  assert.match(controller.elements.connectionBadge.textContent, /FAILED/);
  controller._stopTimer();
  controller.disconnect();
});
