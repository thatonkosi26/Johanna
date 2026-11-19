export async function tradeGet(url) {
  try {
    const response = await fetch(url, { cache: "no-store" });
    if (!response.ok) return { error: await requestFailure(response) };
    return { payload: await response.json() };
  } catch {
    return { error: networkFailure() };
  }
}

// A failed market request as { reason, message }: the backend's own error when it sent one.
export async function requestFailure(response) {
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  return { reason: payload?.error || "refresh_failed", message: payload?.message || `Backend returned ${response.status}` };
}
export const networkFailure = () => ({ reason: "backend_unreachable", message: "The Johanna backend did not answer." });

// GET a JSON endpoint with a timeout; returns { payload } | { error } | { aborted }.
export async function fetchJson(url, poller, timeoutMs) {
  const controller = new AbortController();
  poller.controller = controller;
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", signal: controller.signal });
    if (!response.ok) return { error: await requestFailure(response) };
    try {
      return { payload: await response.json() };
    } catch {
      if (controller.signal.aborted && !timedOut) return { aborted: true };
      return { error: { reason: "malformed_response", message: "The backend sent an unreadable response." } };
    }
  } catch {
    if (timedOut) return { error: { reason: "request_timeout", message: `No answer within ${timeoutMs / 1000} s.` } };
    if (controller.signal.aborted) return { aborted: true };
    return { error: networkFailure() };
  } finally {
    clearTimeout(timer);
  }
}

export const malformed = () => ({ reason: "malformed_response", message: "The backend sent an unexpected response." });
