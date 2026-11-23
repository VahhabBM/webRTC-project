/**
 * Mobile-only video swap (PiP) for the call room.
 *
 * On mobile viewports (≤640 px), one participant's video is shown as the
 * large primary feed and the other as a small picture-in-picture thumbnail.
 * Tapping either video card toggles which is primary and which is PiP.
 *
 * Safety constraints:
 * - No WebRTC, RTCPeerConnection, MediaStream, or track state is modified.
 * - No getUserMedia() call is made.
 * - The swap is purely visual: the `.is-swapped` class is toggled on the
 *   `.videos` container and CSS drives the layout change.
 * - On desktop (viewport > 640 px) the listener is detached and the
 *   `.is-swapped` class is removed so the existing grid layout is preserved.
 * - If the viewport is resized or the device is rotated from mobile to
 *   desktop, the swap state is cleared automatically.
 */

export const MOBILE_BREAKPOINT_QUERY = "(max-width: 640px)";

/**
 * Initialise mobile-only video swap for the given container.
 *
 * @param {HTMLElement} videosContainer
 *   The `.videos` wrapper element that holds both video cards.
 * @param {object} [options]
 *   Dependency-injection hooks used by tests (all optional in production).
 * @param {function(string): MediaQueryList} [options.matchMedia]
 *   Override `window.matchMedia` for testability.
 * @param {string} [options.breakpointQuery]
 *   Override the media-query string (default: MOBILE_BREAKPOINT_QUERY).
 * @returns {{ destroy: function(): void }}
 *   Call `destroy()` to remove all listeners and reset swap state.
 */
export function initVideoSwap(videosContainer, options = {}) {
  const resolvedMatchMedia = options.matchMedia || window.matchMedia.bind(window);
  const query = options.breakpointQuery || MOBILE_BREAKPOINT_QUERY;

  const mq = resolvedMatchMedia(query);
  let attached = false;

  function handleTap(event) {
    // Only respond to taps that land on a video card (or a child of one).
    if (event.target.closest("[data-video]")) {
      videosContainer.classList.toggle("is-swapped");
    }
  }

  function attach() {
    if (!attached) {
      videosContainer.addEventListener("click", handleTap);
      attached = true;
    }
  }

  function detach() {
    if (attached) {
      videosContainer.removeEventListener("click", handleTap);
      // Remove any swap state so the desktop grid layout is never polluted.
      videosContainer.classList.remove("is-swapped");
      attached = false;
    }
  }

  function onBreakpointChange(event) {
    if (event.matches) {
      attach();
    } else {
      detach();
    }
  }

  // Attach immediately if we start on mobile.
  if (mq.matches) {
    attach();
  }

  // React to viewport resizes and device rotations.
  mq.addEventListener("change", onBreakpointChange);

  return {
    /** Remove all listeners and reset visual swap state. */
    destroy() {
      detach();
      mq.removeEventListener("change", onBreakpointChange);
    },
  };
}
