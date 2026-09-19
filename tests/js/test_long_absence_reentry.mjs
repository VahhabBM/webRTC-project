/**
 * T-34: long absence shows a partner-left state; re-entry restores the
 * same round/partner/room without resetting the timer or assigning a replacement.
 */
import assert from "node:assert/strict";
import path from "node:path";
import { afterEach, beforeEach, test } from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../..");
const callRoomHref = pathToFileURL(
  path.join(repoRoot, "apps/events/static/events/js/call_room.js"),
).href;

const callRoomMod = await import(callRoomHref);
const {
  CallRoomController,
  CallRoomPhase,
  PARTNER_GONE_FOOTER,
  computeRemainingMs,
  partnerLeftBanner,
  partnerPresenceFromProtocol,
} = callRoomMod;

function uiElements() {
  return {
    connectionBadge: { textContent: "", className: "", dataset: {} },
    qualityBanner: { hidden: true, textContent: "" },
    partnerGoneBanner: { hidden: true, textContent: "" },
    timerDisplay: { textContent: "", dataset: {} },
    timerContainer: { dataset: {} },
    errorBanner: { hidden: true, textContent: "" },
    localVideo: { srcObject: null },
    remoteVideo: { srcObject: { id: "remote-live" } },
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
    elements,
    createWebSocket: () => ({
      readyState: 1,
      send() {},
      close() {},
    }),
    ...overrides,
  });
  liveControllers.push(controller);
  return { controller, elements };
}

function enterRound(controller, roundEndTs = Date.now() + 120_000) {
  controller.clockSync.offsetMs = 0;
  controller.clockSync.status = "synchronised";
  controller.partner = {
    partnerId: "partner-b",
    roomId: "room-1",
    roundNumber: 1,
    roundStartTs: Date.now(),
    roundEndTs,
    isOfferer: true,
    displayName: "Sara",
    tags: ["ai"],
  };
  controller.roundNumber = 1;
  controller.roundEndTs = roundEndTs;
  controller.partnerPresence = "connected";
  controller._setPhase(CallRoomPhase.IN_ROUND);
  controller._startTimer();
  controller._updatePartnerUI();
  return roundEndTs;
}

beforeEach(() => {
  liveControllers.length = 0;
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
});

test("protocol disconnected maps to a user-facing partner-left state", () => {
  assert.equal(partnerPresenceFromProtocol("disconnected"), "gone");
  assert.equal(partnerPresenceFromProtocol("connected"), "connected");
  assert.match(partnerLeftBanner("Sara"), /Sara left/);
  assert.match(PARTNER_GONE_FOOTER, /round timer continues/);
  assert.doesNotMatch(partnerLeftBanner("Sara"), /error|websocket|ICE/i);
});

test("long absence shows partner left while the same round timer keeps ticking", () => {
  const { controller, elements } = makeController();
  const roundEndTs = enterRound(controller);
  const remainingBefore = computeRemainingMs(roundEndTs, 0, Date.now());

  controller._onPartnerState({
    partner_id: "partner-b",
    state: "disconnected",
    server_ts: Date.now(),
  });

  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.partnerPresence, "gone");
  assert.equal(controller.partner.partnerId, "partner-b");
  assert.equal(controller.partner.roomId, "room-1");
  assert.equal(controller.roundNumber, 1);
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.ok(controller._timerInterval);
  assert.equal(elements.connectionBadge.textContent, "PARTNER LEFT");
  assert.equal(elements.connectionBadge.dataset.quality, "partner-gone");
  assert.match(elements.partnerGoneBanner.textContent, /Sara left/);
  assert.equal(elements.statusFooter.textContent, PARTNER_GONE_FOOTER);
  assert.equal(elements.errorBanner.hidden, true);
  assert.equal(elements.remoteVideo.srcObject, null);
  assert.doesNotMatch(elements.partnerGoneBanner.textContent, /ERR_|ICE|WebSocket/i);

  const remainingDuring = computeRemainingMs(controller.roundEndTs, 0, Date.now());
  assert.ok(remainingDuring <= remainingBefore);
  assert.ok(remainingDuring > remainingBefore - 2000);
  controller.disconnect();
});

test("partner_state from another room is ignored", () => {
  const { controller, elements } = makeController();
  enterRound(controller);
  controller._onPartnerState({
    partner_id: "someone-else",
    state: "disconnected",
    server_ts: Date.now(),
  });
  assert.equal(controller.partnerPresence, "connected");
  assert.equal(elements.connectionBadge.textContent, "");
  assert.equal(elements.partnerGoneBanner.hidden, true);
  controller.disconnect();
});

test("partner return restores the same assignment without starting over", () => {
  const { controller, elements } = makeController();
  const roundEndTs = enterRound(controller);
  controller._onPartnerState({
    partner_id: "partner-b",
    state: "disconnected",
    server_ts: Date.now(),
  });
  controller._onPartnerState({
    partner_id: "partner-b",
    state: "connected",
    server_ts: Date.now(),
  });
  assert.equal(controller.phase, CallRoomPhase.IN_ROUND);
  assert.equal(controller.partnerPresence, "connected");
  assert.equal(controller.partner.roomId, "room-1");
  assert.equal(controller.partner.partnerId, "partner-b");
  assert.equal(controller.roundEndTs, roundEndTs);
  assert.equal(elements.partnerGoneBanner.hidden, true);
  assert.equal(elements.statusFooter.textContent, "Round in progress");
  controller.disconnect();
});

test("stale session error is user-facing and does not look technical", () => {
  const { controller, elements } = makeController();
  enterRound(controller);
  controller._onServerError({
    code: "ERR_ALREADY_CONNECTED",
    message: "You joined from another window. This session is no longer active.",
  });
  assert.match(elements.errorBanner.textContent, /another window/i);
  assert.doesNotMatch(elements.errorBanner.textContent, /traceback|websocket close/i);
  assert.equal(elements.connectionBadge.dataset.quality, "session-replaced");
  controller.disconnect();
});
