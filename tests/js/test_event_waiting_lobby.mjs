import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const moduleUrl = pathToFileURL(
  path.resolve(
    __dirname,
    "../../apps/events/static/events/js/event_waiting_lobby.js",
  ),
);
const {
  estimateServerNowMs,
  formatEventStartCountdown,
  remainingUntilEventStartMs,
  waitForEventStart,
} = await import(moduleUrl);

function fakeIntervalClock() {
  let currentTime = 0;
  let callback;
  let setCalls = 0;
  let clearCalls = 0;
  return {
    now: () => currentTime,
    setIntervalFn(fn) {
      setCalls += 1;
      callback = fn;
      return setCalls;
    },
    clearIntervalFn() {
      clearCalls += 1;
      callback = undefined;
    },
    async advance(milliseconds) {
      currentTime += milliseconds;
      callback?.();
      for (let i = 0; i < 5; i += 1) await Promise.resolve();
    },
    get setCalls() {
      return setCalls;
    },
    get clearCalls() {
      return clearCalls;
    },
  };
}

const BASE_TIME_MS = Date.parse("2026-11-12T19:00:00.000Z");

function participantData(serverOffsetMs, startOffsetMs) {
  return {
    server_time: new Date(BASE_TIME_MS + serverOffsetMs).toISOString(),
    participant: {
      intro_video: {
        event_start_at: new Date(BASE_TIME_MS + startOffsetMs).toISOString(),
      },
    },
  };
}

test("server-time estimate includes half the request time and elapsed time", () => {
  const serverTime = "2026-11-08T19:00:00.000Z";
  const serverTimeMs = Date.parse(serverTime);

  assert.equal(
    estimateServerNowMs(serverTime, 100, 200, 700),
    serverTimeMs + 550,
  );
});

test("remaining time never becomes negative after the scheduled start", () => {
  assert.equal(remainingUntilEventStartMs(2000, 1500), 500);
  assert.equal(remainingUntilEventStartMs(1500, 2000), 0);
});

test("invalid timestamps disable the countdown instead of blocking entry", () => {
  assert.equal(estimateServerNowMs("not-a-time", 100, 200), null);
  assert.equal(remainingUntilEventStartMs("not-a-time", 200), null);
});

test("countdown formats hours, minutes, and seconds for the waiting screen", () => {
  assert.equal(formatEventStartCountdown(2 * 60 * 60 * 1000 + 1500), "02:00:02");
  assert.equal(formatEventStartCountdown(90_001), "00:01:31");
  assert.equal(formatEventStartCountdown(0), "00:00:00");
  assert.equal(formatEventStartCountdown(Number.NaN), "--:--:--");
});

test("postponed then advanced event start is refreshed before opening the room", async () => {
  const clock = fakeIntervalClock();
  const countdowns = [];
  const statuses = [];
  let requestCount = 0;
  let enteredData = null;
  let expectedEnteredData = null;
  const waiting = waitForEventStart(participantData(0, 20_000), {
    initialRequestStartedAtMs: 0,
    initialResponseReceivedAtMs: 0,
    fetchCurrentParticipant: async () => {
      requestCount += 1;
      if (requestCount === 1) return participantData(clock.now(), 60_000);
      expectedEnteredData = participantData(clock.now(), 40_000);
      return expectedEnteredData;
    },
    onCountdown: (value) => countdowns.push(value),
    onStatus: (value) => statuses.push(value),
    now: clock.now,
    setIntervalFn: clock.setIntervalFn,
    clearIntervalFn: clock.clearIntervalFn,
  }).then((data) => {
    enteredData = data;
    return data;
  });

  await clock.advance(15_000);
  assert.equal(requestCount, 1);
  assert.equal(countdowns.at(-1), "00:00:45");
  assert.equal(enteredData, null);
  await clock.advance(15_000);
  assert.equal(requestCount, 2);
  assert.equal(countdowns.at(-1), "00:00:10");
  assert.equal(enteredData, null);
  await clock.advance(15_000);

  assert.equal(requestCount, 3);
  assert.equal(enteredData, expectedEnteredData);
  assert.equal(await waiting, enteredData);
  assert.equal(statuses.at(-1), "");
  assert.equal(clock.setCalls, 1);
  assert.equal(clock.clearCalls, 1);
});

test("network failure at zero retries and never opens the room from stale data", async () => {
  const clock = fakeIntervalClock();
  let requestCount = 0;
  let entered = false;
  let expectedData = null;
  const statuses = [];
  const waiting = waitForEventStart(participantData(0, 2_000), {
    initialRequestStartedAtMs: 0,
    initialResponseReceivedAtMs: 0,
    fetchCurrentParticipant: async () => {
      requestCount += 1;
      if (requestCount === 1) throw new Error("temporary network failure");
      expectedData = participantData(clock.now(), 2_000);
      return expectedData;
    },
    onStatus: (value) => statuses.push(value),
    now: clock.now,
    setIntervalFn: clock.setIntervalFn,
    clearIntervalFn: clock.clearIntervalFn,
  }).then((data) => {
    entered = true;
    return data;
  });

  await clock.advance(1_000);
  await clock.advance(1_000);
  assert.equal(requestCount, 1);
  assert.equal(entered, false);
  assert.match(statuses.at(-1), /retry before opening/);

  await clock.advance(14_999);
  assert.equal(requestCount, 1);
  assert.equal(entered, false);
  await clock.advance(1);

  assert.equal(requestCount, 2);
  assert.equal(entered, true);
  assert.equal(await waiting, expectedData);
  assert.equal(clock.setCalls, 1);
  assert.equal(clock.clearCalls, 1);
});
