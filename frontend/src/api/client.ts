import createClient from "openapi-fetch";
import type { components, paths } from "./schema";
import { endSession, getGeneration, getToken, setSessionEnd, setToken } from "./session";

type TokenOut = components["schemas"]["TokenOut"];

const REFRESH_PATH = "/api/v1/auth/refresh";
const NO_RETRY = ["/api/v1/auth/login", REFRESH_PATH, "/api/v1/auth/logout"];
const RACE_RETRY_DELAY_MS = 300;

export class ApiError extends Error {
  /** `detail` is the server's error body detail: a code string, or a list of field errors on a 422. */
  constructor(
    readonly status: number,
    readonly detail?: unknown,
  ) {
    super(`HTTP ${status}`);
  }
}

async function detailOf(res: Response): Promise<string | undefined> {
  try {
    const body: unknown = await res.clone().json();
    if (typeof body === "object" && body !== null && "detail" in body && typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    /* not JSON */
  }
  return undefined;
}

/** ok = new token stored; rejected = the server refused (session over); network = no answer (session may still be fine). */
export type RefreshResult = "ok" | "rejected" | "network";

async function refreshOnce(): Promise<RefreshResult | "race"> {
  let res: Response;
  try {
    res = await fetch(window.location.origin + REFRESH_PATH, { method: "POST", credentials: "same-origin" });
  } catch {
    return "network";
  }
  if (res.ok) {
    const body = await res.json().then(
      (b: TokenOut) => b,
      () => null,
    );
    if (body === null) return "rejected";
    return storeToken(body) ? "ok" : "rejected";
  }
  // only `refresh_race` is benign (another tab or request rotated the cookie first); every other 401 means the session is over
  return res.status === 401 && (await detailOf(res)) === "refresh_race" ? "race" : "rejected";
}

let startedAt = 0;
function storeToken(body: TokenOut): boolean {
  if (getGeneration() !== startedAt) return false; // the session was cleared (logout) while we were refreshing
  setToken(body.access_token);
  setSessionEnd(body.session_expires_at);
  return true;
}

/** A 401 `refresh_race` is a benign race: retry ONCE (after a short wait) with the cookie the browser holds now. */
async function requestRefresh(): Promise<RefreshResult> {
  startedAt = getGeneration();
  for (let attempt = 0; attempt < 2; attempt++) {
    if (attempt > 0) await new Promise((r) => setTimeout(r, RACE_RETRY_DELAY_MS));
    const out = await refreshOnce();
    if (out !== "race") return out;
  }
  return "rejected";
}

let inflight: Promise<RefreshResult> | null = null;

/** Single-flight: concurrent callers share one refresh request. */
export function refreshSession(): Promise<RefreshResult> {
  inflight ??= requestRefresh().finally(() => {
    inflight = null;
  });
  return inflight;
}

/** Resolves when no refresh is in flight (errors swallowed). Logout waits for it so a late refresh cannot revive the session. */
export function awaitRefresh(): Promise<void> {
  return (inflight ?? Promise.resolve()).then(
    () => undefined,
    () => undefined,
  );
}

async function authedFetch(request: Request): Promise<Response> {
  const retryable = !NO_RETRY.includes(new URL(request.url).pathname);
  const spare = retryable ? request.clone() : null;
  const send = (req: Request): Promise<Response> => {
    const token = getToken();
    if (token) req.headers.set("Authorization", `Bearer ${token}`);
    return fetch(req);
  };

  const sentWith = getToken();
  const res = await send(request);
  if (res.status !== 401 || !spare) return res;
  const detail = await detailOf(res);
  if (detail !== "token_expired" && detail !== "invalid_token") return res;
  if (sentWith !== null && getToken() === null) return res; // a refresh already failed: the session is over

  // another request may already have refreshed the token while this one was in flight
  const alreadyRefreshed = getToken() !== null && getToken() !== sentWith;
  if (!alreadyRefreshed) {
    const out = await refreshSession();
    if (out === "network") return res; // keep the session; the caller shows the error
    if (out === "rejected") {
      endSession();
      return res;
    }
  }
  const again = await send(spare);
  if (again.status === 401) endSession();
  return again;
}

export const api = createClient<paths>({
  baseUrl: window.location.origin,
  credentials: "same-origin",
  fetch: authedFetch,
});

/** Throws ApiError unless the call returned data. */
export async function unwrap<T>(call: Promise<{ data?: T; error?: unknown; response: Response }>): Promise<T> {
  const { data, error, response } = await call;
  if (data === undefined) throw new ApiError(response.status, typeof error === "object" && error !== null && "detail" in error ? error.detail : undefined);
  return data;
}
