/**
 * T-15 client clock synchronization module.
 *
 * Estimates server time via NTP-style offset from client.clock_sync /
 * server.clock_sync exchanges. Never use raw Date.now() alone for
 * round timers — always call estimatedServerNowMs().
 */

export const DEFAULT_SAMPLE_COUNT = 5;
export const DEFAULT_INTERVAL_MS = 30_000;
export const DEFAULT_MAX_RTT_MS = 2000;
export const DEFAULT_MIN_SAMPLES = 3;

/**
 * Compute offset and RTT for a single clock-sync sample.
 * Mirrors apps/events/clock_sync.py make_sample().
 */
export function computeSampleOffset(clientSendMs, serverMs, clientReceiveMs) {
  const rttMs = Math.max(0, clientReceiveMs - clientSendMs);
  const offsetMs = serverMs - (clientSendMs + clientReceiveMs) / 2;
  return { offsetMs, rttMs };
}

/**
 * Median of a numeric array (returns null for empty input).
 */
export function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0
    ? (sorted[mid - 1] + sorted[mid]) / 2
    : sorted[mid];
}

/**
 * Stateful clock-sync estimator. Matches ClockSyncState in clock_sync.py.
 */
export class ClockSyncClient {
  constructor({
    sampleCount = DEFAULT_SAMPLE_COUNT,
    intervalMs = DEFAULT_INTERVAL_MS,
    maxRttMs = DEFAULT_MAX_RTT_MS,
    minSamples = DEFAULT_MIN_SAMPLES,
    now = () => Date.now(),
    monotonicNow = () => performance.now(),
  } = {}) {
    this.sampleCount = sampleCount;
    this.intervalMs = intervalMs;
    this.maxRttMs = maxRttMs;
    this.minSamples = minSamples;
    this._now = now;
    this._monotonicNow = monotonicNow;

    this.samples = [];
    this.offsetMs = null;
    this.rttMs = null;
    this.status = "unsynchronised";
    this._pending = new Map();
    this._intervalId = null;
    this._sendSync = null;
  }

  get isSynchronised() {
    return this.status === "synchronised" && this.offsetMs !== null;
  }

  /**
   * Estimated server time in Unix milliseconds.
   * Returns null until synchronised.
   */
  estimatedServerNowMs(clientNowMs = null) {
    if (this.offsetMs === null) return null;
    const local = clientNowMs ?? this._now();
    return Math.round(local + this.offsetMs);
  }

  /**
   * Begin periodic clock-sync cycles. sendFn receives a T-13 envelope.
   */
  start(sendFn) {
    this._sendSync = sendFn;
    this._runCycle();
    if (this._intervalId) clearInterval(this._intervalId);
    this._intervalId = setInterval(() => this._runCycle(), this.intervalMs);
  }

  stop() {
    if (this._intervalId) {
      clearInterval(this._intervalId);
      this._intervalId = null;
    }
    this._pending.clear();
  }

  /**
   * Queue one client.clock_sync request.
   */
  sendSample() {
    if (!this._sendSync) return;
    const ts = this._now();
    const queue = this._pending.get(ts) || [];
    queue.push(this._monotonicNow());
    this._pending.set(ts, queue);
    this._sendSync({
      type: "client.clock_sync",
      version: 1,
      payload: { client_ts: ts },
    });
  }

  /**
   * Handle server.clock_sync or initial offset from server.hello.
   */
  handleResponse(payload) {
    const clientTsEcho = payload.client_ts_echo;
    if (clientTsEcho === undefined) return false;

    const queue = this._pending.get(clientTsEcho);
    if (!queue || !queue.length) return false;

    const sentMonotonic = queue.shift();
    if (!queue.length) this._pending.delete(clientTsEcho);

    const clientReceive = this._now();
    const { offsetMs, rttMs } = computeSampleOffset(
      clientTsEcho,
      payload.server_ts,
      clientReceive,
    );

    if (rttMs < 0 || rttMs > this.maxRttMs) return false;

    this.samples.push({ offsetMs, rttMs, monotonicRtt: this._monotonicNow() - sentMonotonic });
    return true;
  }

  /**
   * Apply offset hint from server.hello (optional bootstrap before samples).
   */
  applyHelloOffset(clientTs, serverTs, clientReceiveMs = null) {
    const recv = clientReceiveMs ?? this._now();
    const { offsetMs, rttMs } = computeSampleOffset(clientTs, serverTs, recv);
    if (rttMs <= this.maxRttMs) {
      this.samples.push({ offsetMs, rttMs, monotonicRtt: rttMs });
    }
  }

  /**
   * Finalise the current sample batch (median offset/RTT).
   */
  finalizeBatch() {
    if (!this.samples.length) {
      this.status = "unsynchronised";
      this.offsetMs = null;
      this.rttMs = null;
      return;
    }
    const offsets = this.samples.map((s) => s.offsetMs);
    const rtts = this.samples.map((s) => s.rttMs);
    this.offsetMs = median(offsets);
    this.rttMs = median(rtts);
    this.status =
      this.samples.length >= this.minSamples ? "synchronised" : "insufficient";
  }

  _runCycle() {
    this.samples = [];
    this._pending.clear();
    for (let i = 0; i < this.sampleCount; i++) {
      this.sendSample();
    }
  }

  /**
   * Call after each server.clock_sync during an active cycle.
   * Auto-finalises when sampleCount responses are collected.
   */
  onSampleReceived(payload) {
    const accepted = this.handleResponse(payload);
    if (accepted && this.samples.length >= this.sampleCount) {
      this.finalizeBatch();
    }
    return accepted;
  }
}
