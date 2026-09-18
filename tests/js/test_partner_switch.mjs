/**
 * T-31: partner switching reuses one MediaStream and never re-requests devices.
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
  acquireSharedLocalMedia,
  resetSharedLocalMediaForTests,
  getSharedGetUserMediaCallCount,
  TransportState,
} = negotiatorMod;
const { CallRoomController, CallRoomPhase } = callRoomMod;

function makeNegotiator(overrides = {}) {
  return new PerfectNegotiator({
    myParticipantId: "participant-a",
    partnerParticipantId: "participant-b",
    roomId: "room-1",
    sendSignalingMessage: () => {},
    ...overrides,
  });
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

test("getUserMedia is called only once across acquire, rounds, and partner switches", async () => {
  const negotiator = makeNegotiator();
  await negotiator.acquireLocalMedia();
  const stream = negotiator.localStream;
  const tracks = stream.getTracks();

  await negotiator.preconnect("room-1", "participant-b");
  await negotiator.open();
  await negotiator.endRound();
  await negotiator.prepareRound("room-2", "participant-c");
  await negotiator.open();
  await negotiator.switchPartner("room-3", "participant-d");
  await negotiator.acquireLocalMedia();

  assert.equal(getSharedGetUserMediaCallCount(), 1);
  assert.equal(globalThis.__gumCalls, 1);
  assert.equal(negotiator.localStream, stream);
  assert.deepEqual(negotiator.localStream.getTracks(), tracks);
  assert.ok(tracks.every((track) => track.readyState === "live"));
  negotiator.leave();
});

test("shared acquire returns the same MediaStream without a second getUserMedia", async () => {
  const first = await acquireSharedLocalMedia();
  const second = await acquireSharedLocalMedia();
  assert.equal(first, second);
  assert.equal(getSharedGetUserMediaCallCount(), 1);
});

test("acquireLocalMedia does not attach duplicate tracks", async () => {
  const negotiator = makeNegotiator();
  await negotiator.acquireLocalMedia();
  await negotiator.acquireLocalMedia();
  assert.equal(negotiator.pc.addTrackCalls, 2);
  negotiator.leave();
});

test("switchPartner reuses tracks, closes the old PC, and detaches its listeners", async () => {
  const failures = [];
  const negotiator = makeNegotiator({ onFailure: (reason) => failures.push(reason) });
  await negotiator.acquireLocalMedia();
  const stream = negotiator.localStream;
  const oldPc = negotiator.pc;

  await negotiator.switchPartner("room-2", "partner-z");

  assert.equal(negotiator.localStream, stream);
  assert.notEqual(negotiator.pc, oldPc);
  assert.equal(oldPc.closed, true);
  assert.equal(oldPc.onnegotiationneeded, null);
  assert.equal(oldPc.onicecandidate, null);
  assert.equal(oldPc.ontrack, null);
  assert.equal(oldPc.onconnectionstatechange, null);
  assert.equal(negotiator.pc.addTrackCalls, 2);
  assert.equal(negotiator.state, TransportState.OPEN);
  assert.equal(getSharedGetUserMediaCallCount(), 1);

  oldPc.connectionState = "failed";
  oldPc.onconnectionstatechange?.();
  assert.deepEqual(failures, []);
  negotiator.leave();
});

test("prepareRound keeps local video tracks enabled for preview", async () => {
  const negotiator = makeNegotiator();
  await negotiator.acquireLocalMedia();
  const video = negotiator.localStream.getVideoTracks()[0];
  await negotiator.open();
  await negotiator.endRound();
  await negotiator.prepareRound("room-2", "partner-c");
  assert.equal(video.readyState, "live");
  assert.equal(video.enabled, true);
  assert.equal(negotiator.state, TransportState.PRECONNECTED);
  negotiator.leave();
});

test("switchPartner failure cleans up only the peer connection and keeps media", async () => {
  const failures = [];
  const negotiator = makeNegotiator({ onFailure: (reason) => failures.push(reason) });
  await negotiator.acquireLocalMedia();
  const stream = negotiator.localStream;
  negotiator._initPeerConnection = () => {
    throw new Error("simulated pc init failure");
  };

  await assert.rejects(() => negotiator.switchPartner("room-2", "partner-c"));

  assert.equal(negotiator.pc, null);
  assert.equal(negotiator.localStream, stream);
  assert.ok(stream.getTracks().every((track) => track.readyState === "live"));
  assert.deepEqual(failures, ["PARTNER_SWITCH_FAILED"]);
  assert.notEqual(negotiator.state, TransportState.CLOSED);
  negotiator.leave();
});

test("leave() still stops hardware tracks and stats timers", async () => {
  const negotiator = makeNegotiator();
  await negotiator.acquireLocalMedia();
  const tracks = negotiator.localStream.getTracks();
  negotiator._startStatsMonitor();
  assert.ok(negotiator.statsInterval);

  negotiator.leave();

  assert.equal(negotiator.pc, null);
  assert.equal(negotiator.localStream, null);
  assert.equal(negotiator.statsInterval, null);
  assert.equal(negotiator.state, TransportState.CLOSED);
  assert.ok(tracks.every((track) => track.readyState === "ended"));
});

test("call room keeps one stream and local preview across three rounds", async () => {
  const localVideo = { srcObject: null };
  const remoteVideo = { srcObject: null };
  const errorBanner = { hidden: true, textContent: "" };
  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    elements: { localVideo, remoteVideo, errorBanner },
  });

  await controller.ensureLocalMedia();
  const stream = controller._sharedStream;
  assert.equal(localVideo.srcObject, stream);
  assert.equal(getSharedGetUserMediaCallCount(), 1);

  await controller._onPairing(pairingPayload(1, "room-1", "partner-b"));
  await controller._onRoundStart({ round_number: 1 });
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.negotiator.localStream, stream);
  assert.equal(localVideo.srcObject, stream);

  await controller._onRoundEnd({ round_number: 1 });
  assert.equal(remoteVideo.srcObject, null);
  assert.equal(localVideo.srcObject, stream);
  assert.ok(stream.getVideoTracks()[0].readyState === "live");

  await controller._onPairing(pairingPayload(2, "room-2", "partner-c"));
  await controller._onRoundStart({ round_number: 2 });
  await controller._onRoundEnd({ round_number: 2 });
  await controller._onPairing(pairingPayload(3, "room-3", "partner-d"));
  await controller._onRoundStart({ round_number: 3 });

  assert.equal(getSharedGetUserMediaCallCount(), 1);
  assert.equal(globalThis.__gumCalls, 1);
  assert.equal(controller.negotiator.localStream, stream);
  assert.equal(localVideo.srcObject, stream);
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(errorBanner.hidden, true);

  await controller._onEventEnd({ reason: "completed" });
  assert.equal(controller.phase, CallRoomPhase.EVENT_ENDED);
  assert.equal(controller.negotiator, null);
  assert.equal(localVideo.srcObject, null);
  assert.ok(stream.getTracks().every((track) => track.readyState === "ended"));
});

test("partner-switch failure shows an error and does not stop the session media", async () => {
  const stream = new FakeStream([new FakeTrack("audio"), new FakeTrack("video")]);
  const localVideo = { srcObject: stream };
  const errorBanner = { hidden: true, textContent: "" };
  const connectionBadge = { textContent: "", className: "" };
  let left = false;
  let acquireCount = 0;

  const fakeNegotiator = {
    localStream: stream,
    roomId: "room-1",
    partnerId: "partner-b",
    pc: { signalingState: "stable", connectionState: "connected" },
    acquireLocalMedia: async () => {
      acquireCount += 1;
      fakeNegotiator.localStream = stream;
    },
    preconnect: async () => {},
    createManualOffer: async () => {},
    prepareRound: async () => {
      throw new Error("simulated switch failure");
    },
    open: async () => {},
    endRound: async () => {},
    leave: () => {
      left = true;
    },
    handleSignalingMessage: async () => {},
  };

  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    elements: { localVideo, errorBanner, connectionBadge, remoteVideo: { srcObject: null } },
    negotiatorFactory: () => fakeNegotiator,
  });
  controller._sharedStream = stream;

  await controller._onPairing(pairingPayload(1, "room-1", "partner-b"));
  await controller._onRoundStart({ round_number: 1 });
  await controller._onRoundEnd({ round_number: 1 });
  await controller._onPairing(pairingPayload(2, "room-2", "partner-c"));

  assert.equal(acquireCount, 1);
  assert.equal(left, false);
  assert.equal(controller.negotiator, fakeNegotiator);
  assert.equal(localVideo.srcObject, stream);
  assert.equal(errorBanner.hidden, false);
  assert.match(errorBanner.textContent, /camera stays on/i);
  assert.notEqual(controller.phase, CallRoomPhase.EVENT_ENDED);
  assert.ok(stream.getTracks().every((track) => track.readyState === "live"));
  controller._stopTimer();
});

test("ensureLocalMedia on reconnect does not call getUserMedia again", async () => {
  const controller = new CallRoomController({
    myParticipantId: "participant-a",
    elements: { localVideo: { srcObject: null } },
  });
  await controller.ensureLocalMedia();
  await controller.ensureLocalMedia();
  assert.equal(getSharedGetUserMediaCallCount(), 1);
  controller.disconnect();
});
