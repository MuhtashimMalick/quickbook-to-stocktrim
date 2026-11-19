// The access token lives in this module variable ONLY: never localStorage, sessionStorage or a cookie.
let accessToken: string | null = null;
let generation = 0; // bumped whenever the session is cleared, so a refresh that finishes later cannot revive it
const lostListeners = new Set<() => void>();
const endListeners = new Set<() => void>();
let sessionEnd: number | null = null; // absolute end of the session (ms since epoch), from the login/refresh response

export const getToken = (): string | null => accessToken;
export const getGeneration = (): number => generation;

export function setToken(token: string | null): void {
  accessToken = token;
  if (token === null) {
    generation++;
    setSessionEnd(null);
  }
}

export const getSessionEnd = (): number | null => sessionEnd;

export function setSessionEnd(iso: string | null): void {
  const next = iso === null ? null : Date.parse(iso);
  sessionEnd = next !== null && Number.isFinite(next) ? next : null;
  endListeners.forEach((fn) => fn());
}

export function onSessionEnd(fn: () => void): () => void {
  endListeners.add(fn);
  return () => endListeners.delete(fn);
}

/** Definitive loss of the session (refresh rejected): clear the token and tell the app once. */
export function endSession(): void {
  if (accessToken === null) return; // already ended
  setToken(null);
  lostListeners.forEach((fn) => fn());
}

export function onSessionLost(fn: () => void): () => void {
  lostListeners.add(fn);
  return () => lostListeners.delete(fn);
}
