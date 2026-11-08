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
