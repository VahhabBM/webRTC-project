import assert from "node:assert/strict";
import path from "node:path";
import { afterEach, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

class FakeStream {
  getTracks() { return []; }
}

class FakePC {
  constructor(config) {
    this.config = config;
    this.connectionState = "new";
    this.signalingState = "stable";
    this._senders = [];
    FakePC.instances.push(this);
  }
  getSenders() { return this._senders; }
  removeTrack(sender) { this._senders = this._senders.filter((item) => item !== sender); }
  close() { this.connectionState = "closed"; }
  async getStats() { return new Map(); }
  getTransceivers() { return []; }
  async setParameters() {}
}
FakePC.instances = [];

globalThis.MediaStream = FakeStream;
globalThis.RTCPeerConnection = FakePC;

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const negotiatorHref = pathToFileURL(
  path.join(repoRoot, "apps/events/static/events/js/perfect_negotiator.js"),
).href;
const { PerfectNegotiator, resetSharedLocalMediaForTests } = await import(negotiatorHref);
const originalFetch = globalThis.fetch;

function createNegotiator() {
  return new PerfectNegotiator({
    myParticipantId: "participant-a",
    partnerParticipantId: "participant-b",
    roomId: "room-a",
    sendSignalingMessage: () => {},
  });
}

function inboundStats({ audioBytes = 0, decodedFrames = 0, receivedFrames = 0 } = {}) {
  return new Map([
    ["audio", { type: "inbound-rtp", kind: "audio", bytesReceived: audioBytes }],
    ["video", {
      type: "inbound-rtp",
      kind: "video",
      framesDecoded: decodedFrames || undefined,
      framesReceived: receivedFrames,
    }],
    ["pair", {
      type: "candidate-pair",
      state: "succeeded",
      selected: true,
      localCandidateId: "local",
      remoteCandidateId: "remote",
    }],
    ["local", { type: "local-candidate", candidateType: "host" }],
    ["remote", { type: "remote-candidate", candidateType: "host" }],
  ]);
}

afterEach(() => {
  resetSharedLocalMediaForTests();
  if (originalFetch === undefined) delete globalThis.fetch;
  else globalThis.fetch = originalFetch;
  FakePC.instances = [];
});

test("media receipt posts only after inbound audio bytes and decoded video frames are positive", async () => {
  const requests = [];
  globalThis.fetch = async (_url, options) => {
    requests.push(JSON.parse(options.body));
    return { ok: true };
  };
  const negotiator = createNegotiator();

  assert.equal(await negotiator._reportInboundMedia(inboundStats({ decodedFrames: 5 })), false);
  assert.equal(await negotiator._reportInboundMedia(inboundStats({ audioBytes: 64 })), false);
  assert.equal(requests.length, 0);

  assert.equal(await negotiator._reportInboundMedia(inboundStats({ audioBytes: 64, decodedFrames: 5 })), true);
  assert.deepEqual({
    ...requests[0],
    connection_time_ms: undefined,
  }, {
    room_id: "room-a",
    connection_type: "direct",
    connection_time_ms: undefined,
    audio_bytes_received: 64,
    video_frames_decoded: 5,
  });
  assert.equal(Number.isInteger(requests[0].connection_time_ms), true);
  assert.ok(requests[0].connection_time_ms >= 0);
  assert.equal(negotiator._mediaReceivedReported, true);
  assert.equal(await negotiator._reportInboundMedia(inboundStats({ audioBytes: 80, decodedFrames: 9 })), false);
  assert.equal(requests.length, 1);
  negotiator.leave();
});

test("media receipt report retries after a failed HTTP response and supports framesReceived fallback", async () => {
  let attempts = 0;
  globalThis.fetch = async () => ({ ok: ++attempts > 1 });
  const negotiator = createNegotiator();
  const stats = inboundStats({ audioBytes: 70, receivedFrames: 7 });

  assert.equal(await negotiator._reportInboundMedia(stats), false);
  assert.equal(negotiator._mediaReceivedReported, false);
  assert.equal(await negotiator._reportInboundMedia(stats), true);
  assert.equal(attempts, 2);
  assert.equal(negotiator._mediaReceivedReported, true);
  negotiator.leave();
});

test("switching peers aborts an in-flight proof and cannot mark the next room reported", async () => {
  let requestSignal;
  let resolveRequest;
  let requestStartedResolve;
  const requestStarted = new Promise((resolve) => { requestStartedResolve = resolve; });
  globalThis.fetch = (_url, options) => {
    requestSignal = options.signal;
    requestStartedResolve();
    return new Promise((resolve) => { resolveRequest = resolve; });
  };
  const negotiator = createNegotiator();
  const report = negotiator._reportInboundMedia(
    inboundStats({ audioBytes: 100, decodedFrames: 10 }),
  );
  await requestStarted;

  await negotiator.switchPartner("room-b", "participant-c");
  assert.equal(requestSignal.aborted, true);
  resolveRequest({ ok: true });
  assert.equal(await report, false);
  assert.equal(negotiator.roomId, "room-b");
  assert.equal(negotiator._mediaReceivedReported, false);
  negotiator.leave();
});
