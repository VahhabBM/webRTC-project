export function estimateServerNowMs(
  serverTime,
  requestStartedAtMs,
  responseReceivedAtMs,
  clientNowMs = responseReceivedAtMs,
) {
  const serverTimeMs = Date.parse(serverTime);
  const requestStartedAt = Number(requestStartedAtMs);
  const responseReceivedAt = Number(responseReceivedAtMs);
  const clientNow = Number(clientNowMs);

  if (
    !Number.isFinite(serverTimeMs) ||
    !Number.isFinite(requestStartedAt) ||
    !Number.isFinite(responseReceivedAt) ||
    !Number.isFinite(clientNow)
  ) {
    return null;
  }

  const roundTripMs = Math.max(0, responseReceivedAt - requestStartedAt);
  const elapsedSinceResponseMs = Math.max(0, clientNow - responseReceivedAt);
  return serverTimeMs + roundTripMs / 2 + elapsedSinceResponseMs;
}

export function remainingUntilEventStartMs(eventStartAt, estimatedServerNowMs) {
  const eventStartMs =
    typeof eventStartAt === "number" ? eventStartAt : Date.parse(eventStartAt);
  const serverNowMs = Number(estimatedServerNowMs);

  if (!Number.isFinite(eventStartMs) || !Number.isFinite(serverNowMs)) {
    return null;
  }

  return Math.max(0, eventStartMs - serverNowMs);
}

export function formatEventStartCountdown(remainingMs) {
  if (!Number.isFinite(remainingMs)) {
    return "--:--:--";
  }

  const totalSeconds = Math.max(0, Math.ceil(remainingMs / 1000));
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function waitForEventStart(
  initialData,
  {
    initialRequestStartedAtMs,
    initialResponseReceivedAtMs,
    fetchCurrentParticipant,
    onCountdown = () => {},
    onStatus = () => {},
    pollIntervalMs = 15_000,
    tickIntervalMs = 1_000,
    now = () => performance.now(),
    setIntervalFn = (callback, delay) => window.setInterval(callback, delay),
    clearIntervalFn = (timerId) => window.clearInterval(timerId),
  },
) {
  return new Promise((resolve) => {
    let latestData = initialData;
    let requestStartedAt = initialRequestStartedAtMs;
    let responseReceivedAt = initialResponseReceivedAtMs;
    let lastPollAt = now();
    let nextPollAt = lastPollAt + pollIntervalMs;
    let requestInFlight = false;
    let finished = false;
    let zeroReached = false;
    let intervalId;

    const remaining = (clientNow) => {
      const serverNow = estimateServerNowMs(
        latestData?.server_time,
        requestStartedAt,
        responseReceivedAt,
        clientNow,
      );
      const startAt =
        latestData?.participant?.intro_video?.event_start_at ||
        latestData?.participant?.event_start_at;
      return remainingUntilEventStartMs(startAt, serverNow);
    };

    if (remaining(lastPollAt) === null) nextPollAt = lastPollAt;

    const finish = (data) => {
      if (finished) return;
      finished = true;
      clearIntervalFn(intervalId);
      resolve(data);
    };

    const refresh = async () => {
      if (requestInFlight || finished) return;
      requestInFlight = true;
      const requestStarted = now();
      try {
        const refreshedData = await fetchCurrentParticipant();
        const responseReceived = now();
        if (!refreshedData?.server_time || !refreshedData?.participant) {
          throw new Error("Participant schedule is unavailable");
        }
        latestData = refreshedData;
        requestStartedAt = requestStarted;
        responseReceivedAt = responseReceived;
        lastPollAt = responseReceived;
        nextPollAt = responseReceived + pollIntervalMs;
        const refreshedRemaining = remaining(responseReceived);
        if (refreshedRemaining === null) {
          zeroReached = false;
          onStatus("We could not verify the event start time. Rechecking shortly…");
        } else if (refreshedRemaining === 0) {
          zeroReached = true;
          onCountdown("00:00:00");
          onStatus("");
          finish(latestData);
        } else {
          zeroReached = false;
          onCountdown(formatEventStartCountdown(refreshedRemaining));
          onStatus("Your event has not started yet.");
        }
      } catch (_error) {
        lastPollAt = now();
        nextPollAt = lastPollAt + pollIntervalMs;
        onStatus("Connection unavailable. We’ll retry before opening your camera.");
      } finally {
        requestInFlight = false;
      }
    };

    const tick = () => {
      if (finished || requestInFlight) return;
      const clientNow = now();
      const remainingMs = remaining(clientNow);
      if (remainingMs !== null) {
        onCountdown(formatEventStartCountdown(remainingMs));
      }

      // A zero countdown is only a signal to recheck. The camera stays closed
      // until a fresh response confirms that the current server schedule began.
      if (remainingMs === 0) {
        onStatus("Checking the latest event start time…");
        if (!zeroReached || clientNow >= nextPollAt) {
          zeroReached = true;
          void refresh();
        }
      } else if (clientNow >= nextPollAt) {
        zeroReached = false;
        void refresh();
      } else if (remainingMs === null) {
        onStatus("We could not verify the event start time. Rechecking shortly…");
      }
    };

    intervalId = setIntervalFn(tick, tickIntervalMs);
    onStatus("Your event has not started yet.");
    tick();
  });
}
