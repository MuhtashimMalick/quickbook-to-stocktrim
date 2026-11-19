import { useInfiniteQuery, useQuery, useQueryClient, type InfiniteData } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api, unwrap } from "../../api/client";
import { keys, type EventScope } from "../../api/queryKeys";
import type { CycleDetail, CycleOut, EventOut, PushRunOut, RunListItem } from "../../api/types";
import { usePollInterval } from "../../lib/polling";

export const PAGE_SIZE = 25;
const EVENT_PAGE = 200;

const isRunning = (x?: { status: string }) => x?.status === "running";

/** The reason catalog (code to title / explanation / next step). It never changes while the app is open. */
export function useReasonCodes() {
  return useQuery({
    queryKey: keys.reasons,
    queryFn: () => unwrap(api.GET("/api/v1/reason-codes")),
    staleTime: Infinity,
  });
}

/** Title for a reason or status code; falls back to the raw code so an unknown one is still visible. */
export function useReasonTitle(): (code: string | null | undefined) => string {
  const { data } = useReasonCodes();
  return (code) => (code ? (data?.[code]?.title ?? code) : "");
}

export function useRecentCycles(limit: number, live: boolean) {
  const refetchInterval = usePollInterval<CycleOut[]>(live);
  return useQuery({
    queryKey: keys.recentCycles(limit),
    queryFn: () => unwrap(api.GET("/api/v1/cycles", { params: { query: { limit } } })),
    refetchInterval,
  });
}

/** Cycles newest first; "Load more" passes the id of the last row as the `before` cursor. */
export function useCycleList() {
  const refetchInterval = usePollInterval<InfiniteData<CycleOut[]>>((d) => d?.pages[0]?.some(isRunning) ?? false);
  return useInfiniteQuery({
    queryKey: keys.cycleList,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => unwrap(api.GET("/api/v1/cycles", { params: { query: { limit: PAGE_SIZE, before: pageParam } } })),
    getNextPageParam: (last) => (last.length < PAGE_SIZE ? undefined : last[last.length - 1]?.id),
    refetchInterval,
  });
}

export function useRunList() {
  const refetchInterval = usePollInterval<InfiniteData<RunListItem[]>>((d) => d?.pages[0]?.some(isRunning) ?? false);
  return useInfiniteQuery({
    queryKey: keys.runList,
    initialPageParam: undefined as string | undefined,
    queryFn: ({ pageParam }) => unwrap(api.GET("/api/v1/push/runs", { params: { query: { limit: PAGE_SIZE, before: pageParam } } })),
    getNextPageParam: (last) => (last.length < PAGE_SIZE ? undefined : last[last.length - 1]?.id),
    refetchInterval,
  });
}

export function useCycle(id: string) {
  const refetchInterval = usePollInterval<CycleDetail>(isRunning);
  return useQuery({
    queryKey: keys.cycle(id),
    queryFn: () => unwrap(api.GET("/api/v1/cycles/{cycle_id}", { params: { path: { cycle_id: id } } })),
    refetchInterval,
  });
}

/** Progress of a push run (counts per entity, ETA). Disabled without an id. */
export function usePushRun(id: string | null | undefined) {
  const refetchInterval = usePollInterval<PushRunOut>(isRunning);
  return useQuery({
    queryKey: keys.run(id ?? ""),
    queryFn: () => unwrap(api.GET("/api/v1/push/runs/{run_id}", { params: { path: { run_id: id ?? "" } } })),
    enabled: Boolean(id),
    refetchInterval,
  });
}

export interface EventsState {
  items: EventOut[];
  last_id: number;
}

/**
 * Events of one cycle or run, fetched incrementally: each poll asks only for ids after the last one seen
 * and appends them (deduplicated by id), so nothing is fetched twice. Polls fast while `live`, and once more when it ends.
 */
export function useEvents(scope: EventScope, live: boolean) {
  const qc = useQueryClient();
  const key = keys.events(scope);
  const refetchInterval = usePollInterval<EventsState>(live);
  const q = useQuery({
    queryKey: key,
    enabled: Boolean(scope.cycle_id ?? scope.run_id),
    refetchInterval,
    queryFn: async (): Promise<EventsState> => {
      const prev = qc.getQueryData<EventsState>(key);
      let items = prev?.items ?? [];
      let last = prev?.last_id ?? 0;
      for (;;) {
        const page = await unwrap(api.GET("/api/v1/events", { params: { query: { ...scope, after_id: last, limit: EVENT_PAGE } } }));
        const known = new Set(items.map((e) => e.id));
        items = [...items, ...page.items.filter((e) => !known.has(e.id))];
        last = page.last_id;
        if (page.items.length < EVENT_PAGE) return { items, last_id: last };
      }
    },
  });
  const wasLive = useRef(live);
  const { refetch } = q;
  useEffect(() => {
    if (wasLive.current && !live) void refetch(); // the run just ended: fetch its final events
    wasLive.current = live;
  }, [live, refetch]);
  return q;
}

