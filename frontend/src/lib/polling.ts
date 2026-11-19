import { useSyncExternalStore } from "react";

export const POLL_ACTIVE_MS = 2000;
export const POLL_IDLE_MS = 30_000;

const subscribe = (cb: () => void) => {
  document.addEventListener("visibilitychange", cb);
  return () => document.removeEventListener("visibilitychange", cb);
};

/** False while the browser tab is hidden. */
export function useTabVisible(): boolean {
  return useSyncExternalStore(subscribe, () => document.visibilityState !== "hidden", () => true);
}

type Activity<T> = boolean | ((data: T | undefined) => boolean);

/**
 * A `refetchInterval` for TanStack Query: 2 s while something is running, 30 s otherwise, and none at all while the tab is hidden.
 * Pass a boolean, or a function of the query's data when the activity comes from the data itself.
 */
export function usePollInterval<T = unknown>(isActive: Activity<T>): (query: { state: { data: T | undefined } }) => number | false {
  const visible = useTabVisible();
  return (query) => {
    if (!visible) return false;
    const active = typeof isActive === "function" ? isActive(query.state.data) : isActive;
    return active ? POLL_ACTIVE_MS : POLL_IDLE_MS;
  };
}
