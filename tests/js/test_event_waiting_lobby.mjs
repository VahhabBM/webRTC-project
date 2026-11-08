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
} = await import(moduleUrl);

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
