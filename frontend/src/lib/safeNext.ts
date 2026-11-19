const hasUnsafeChar = (s: string): boolean => [...s].some((c) => c === "\\" || c.charCodeAt(0) < 32);

/** Post-login destination: only a same-origin relative path is allowed (no absolute URL, no `//host`, no backslashes). */
export function safeNext(next: string | null | undefined, fallback = "/"): string {
  if (!next || !next.startsWith("/") || next.startsWith("//") || hasUnsafeChar(next)) return fallback;
  try {
    const url = new URL(next, window.location.origin);
    if (url.origin !== window.location.origin || url.pathname === "/login") return fallback;
    return url.pathname + url.search + url.hash;
  } catch {
    return fallback;
  }
}
