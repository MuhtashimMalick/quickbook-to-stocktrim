import { useEffect, useState } from "react";

export const STALE_AFTER_FAILURES = 3;

interface Counters {
  errorUpdateCount: number;
  dataUpdatedAt: number;
}

/** Consecutive failed refreshes since the last success. */
export function useConsecutiveFailures(q: Counters): number {
  const [seen, setSeen] = useState({ errors: q.errorUpdateCount, data: q.dataUpdatedAt, failures: 0 });
  if (q.errorUpdateCount !== seen.errors || q.dataUpdatedAt !== seen.data) {
    const failures = q.dataUpdatedAt !== seen.data ? 0 : seen.failures + (q.errorUpdateCount - seen.errors);
    setSeen({ errors: q.errorUpdateCount, data: q.dataUpdatedAt, failures });
  }
  return seen.failures;
}

/** A clock that re-renders its component every `tickMs` (relative times and running durations). */
export function useNow(tickMs = 30_000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), tickMs);
    return () => clearInterval(id);
  }, [tickMs]);
  return now;
}
