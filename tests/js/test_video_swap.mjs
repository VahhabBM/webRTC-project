/**
 * T-34 (mobile video swap): unit tests for initVideoSwap.
 *
 * Verifies:
 *   - Mobile: click listener attached; tapping a [data-video] card toggles
 *     the `.is-swapped` class on the container.
 *   - Mobile: swap is reversible and repeatable.
 *   - Desktop: no listener is attached; clicking does not change layout.
 *   - Resize from mobile to desktop: listener is detached, .is-swapped
 *     is removed.
 *   - Resize from desktop to mobile: listener is attached.
 *   - destroy(): cleans up all listeners and resets swap state.
 *
 * Uses lightweight DOM/matchMedia stubs only — no browser or jsdom required.
 */

import assert from "node:assert/strict";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { test } from "node:test";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const moduleUrl = pathToFileURL(
  path.resolve(__dirname, "../../apps/events/static/events/js/video_swap.js"),
);
const { initVideoSwap, MOBILE_BREAKPOINT_QUERY } = await import(moduleUrl);

// ---------------------------------------------------------------------------
// Minimal DOM stubs
// ---------------------------------------------------------------------------

/**
 * Returns an object that mimics an HTMLElement well enough for initVideoSwap.
 * Supports classList.add/remove/toggle/contains and addEventListener/
 * removeEventListener with a single event name ("click").
 */
function makeContainer() {
  const classes = new Set();
  const listeners = { click: [] };

  const container = {
    classList: {
      add(c) {
        classes.add(c);
      },
      remove(c) {
        classes.delete(c);
      },
      toggle(c) {
        if (classes.has(c)) {
          classes.delete(c);
        } else {
          classes.add(c);
        }
      },
      contains(c) {
        return classes.has(c);
      },
    },
    addEventListener(type, fn) {
      if (!listeners[type]) listeners[type] = [];
      listeners[type].push(fn);
    },
    removeEventListener(type, fn) {
      if (listeners[type]) {
        listeners[type] = listeners[type].filter((f) => f !== fn);
      }
    },
    /** Test helper: dispatch a fake click from an element with [data-video]. */
    simulateVideoTap() {
      const fakeTarget = makeVideoCardTarget();
      const event = { target: fakeTarget };
      for (const fn of listeners.click ?? []) fn(event);
    },
    /** Test helper: dispatch a click from outside any video card. */
    simulateBackgroundTap() {
      const fakeTarget = makeNonVideoTarget();
      const event = { target: fakeTarget };
      for (const fn of listeners.click ?? []) fn(event);
    },
    _listenerCount(type) {
      return (listeners[type] ?? []).length;
    },
  };

  return container;
}

/** Fake event target whose .closest("[data-video]") resolves to itself. */
function makeVideoCardTarget() {
  return {
    closest(selector) {
      if (selector === "[data-video]") return this;
      return null;
    },
  };
}

/** Fake event target whose .closest("[data-video]") returns null. */
function makeNonVideoTarget() {
  return {
    closest() {
      return null;
    },
  };
}

/**
 * Returns a fake matchMedia factory.
 *
 * @param {boolean} initiallyMobile - whether the fake MQ starts as "matched"
 * @returns {{ factory: function, trigger: function }}
 *   factory: pass as options.matchMedia to initVideoSwap
 *   trigger: call with true/false to simulate a viewport change
 */
function makeFakeMatchMedia(initiallyMobile) {
  let currentMatches = initiallyMobile;
  const changeListeners = [];

  const mql = {
    get matches() {
      return currentMatches;
    },
    addEventListener(type, fn) {
      if (type === "change") changeListeners.push(fn);
    },
    removeEventListener(type, fn) {
      const i = changeListeners.indexOf(fn);
      if (i !== -1) changeListeners.splice(i, 1);
    },
  };

  function factory(_query) {
    return mql;
  }

  function trigger(newMatches) {
    currentMatches = newMatches;
    for (const fn of changeListeners) fn({ matches: newMatches });
  }

  return { factory, trigger, mql };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("MOBILE_BREAKPOINT_QUERY is the expected string", () => {
  assert.equal(MOBILE_BREAKPOINT_QUERY, "(max-width: 640px)");
});

test("mobile: initVideoSwap attaches a click listener", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(true);
  initVideoSwap(container, { matchMedia: factory });

  assert.equal(container._listenerCount("click"), 1);
});

test("mobile: tapping a video card toggles .is-swapped on the container", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(true);
  initVideoSwap(container, { matchMedia: factory });

  assert.equal(container.classList.contains("is-swapped"), false);
  container.simulateVideoTap();
  assert.equal(container.classList.contains("is-swapped"), true);
});

test("mobile: second tap removes .is-swapped (swap is reversible)", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(true);
  initVideoSwap(container, { matchMedia: factory });

  container.simulateVideoTap();
  assert.equal(container.classList.contains("is-swapped"), true);

  container.simulateVideoTap();
  assert.equal(container.classList.contains("is-swapped"), false);
});

test("mobile: swap is repeatable (multiple toggles)", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(true);
  initVideoSwap(container, { matchMedia: factory });

  for (let i = 0; i < 6; i++) {
    container.simulateVideoTap();
    assert.equal(
      container.classList.contains("is-swapped"),
      i % 2 === 0,
      `expected is-swapped=${i % 2 === 0} after tap ${i + 1}`,
    );
  }
});

test("mobile: tapping outside a video card does not toggle .is-swapped", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(true);
  initVideoSwap(container, { matchMedia: factory });

  container.simulateBackgroundTap();
  assert.equal(container.classList.contains("is-swapped"), false);
});

test("desktop: no click listener is attached on init", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(false);
  initVideoSwap(container, { matchMedia: factory });

  assert.equal(container._listenerCount("click"), 0);
});

test("desktop: clicking does not change container class", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(false);
  initVideoSwap(container, { matchMedia: factory });

  // Simulate a click even though no real listener was registered.
  container.simulateVideoTap();
  assert.equal(container.classList.contains("is-swapped"), false);
});

test("resize mobile→desktop: listener is detached and .is-swapped is cleared", () => {
  const container = makeContainer();
  const { factory, trigger } = makeFakeMatchMedia(true);
  initVideoSwap(container, { matchMedia: factory });

  // Swap on mobile first.
  container.simulateVideoTap();
  assert.equal(container.classList.contains("is-swapped"), true);

  // Resize to desktop.
  trigger(false);

  assert.equal(container._listenerCount("click"), 0, "listener should be removed");
  assert.equal(
    container.classList.contains("is-swapped"),
    false,
    ".is-swapped should be cleared",
  );
});

test("resize desktop→mobile: listener is attached", () => {
  const container = makeContainer();
  const { factory, trigger } = makeFakeMatchMedia(false);
  initVideoSwap(container, { matchMedia: factory });

  assert.equal(container._listenerCount("click"), 0);

  // Resize to mobile.
  trigger(true);

  assert.equal(container._listenerCount("click"), 1);

  // And swapping should now work.
  container.simulateVideoTap();
  assert.equal(container.classList.contains("is-swapped"), true);
});

test("resize mobile→desktop→mobile: listener toggled correctly each time", () => {
  const container = makeContainer();
  const { factory, trigger } = makeFakeMatchMedia(true);
  initVideoSwap(container, { matchMedia: factory });

  assert.equal(container._listenerCount("click"), 1);

  trigger(false);
  assert.equal(container._listenerCount("click"), 0);

  trigger(true);
  assert.equal(container._listenerCount("click"), 1);

  trigger(false);
  assert.equal(container._listenerCount("click"), 0);
});

test("destroy(): removes the click listener and clears .is-swapped", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(true);
  const handle = initVideoSwap(container, { matchMedia: factory });

  container.simulateVideoTap();
  assert.equal(container.classList.contains("is-swapped"), true);

  handle.destroy();

  assert.equal(container._listenerCount("click"), 0);
  assert.equal(container.classList.contains("is-swapped"), false);
});

test("destroy(): clicking after destroy does not change container state", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(true);
  const handle = initVideoSwap(container, { matchMedia: factory });

  handle.destroy();

  container.simulateVideoTap();
  assert.equal(container.classList.contains("is-swapped"), false);
});

test("destroy() on desktop: does not throw and is a no-op", () => {
  const container = makeContainer();
  const { factory } = makeFakeMatchMedia(false);
  const handle = initVideoSwap(container, { matchMedia: factory });

  assert.doesNotThrow(() => handle.destroy());
  assert.equal(container._listenerCount("click"), 0);
});
