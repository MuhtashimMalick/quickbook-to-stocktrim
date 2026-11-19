import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api, unwrap } from "../../api/client";
import { keys } from "../../api/queryKeys";
import type { EntriesOut, EntryOut, RunListItem } from "../../api/types";
import { useTabVisible } from "../../lib/polling";
import { pushRunOptions } from "../runs/api";
import { OUTCOMES, PAGE_SIZE, parseEntryId, type EntryParams, entryId } from "./params";

const RECENT_RUNS = 25;
const SLOW_POLL_MS = 30_000;

/** The newest real (non-dry) run; with only dry runs, the newest run. */
export const defaultRun = (runs: RunListItem[] | undefined): RunListItem | undefined => runs?.find((r) => !r.dry_run) ?? runs?.[0];

/** Recent runs for the run picker. No polling: it refreshes when the window regains focus. */
export function useRecentRuns() {
  return useQuery({
    queryKey: keys.recentRuns(RECENT_RUNS),
    queryFn: () => unwrap(api.GET("/api/v1/push/runs", { params: { query: { limit: RECENT_RUNS } } })),
    refetchOnWindowFocus: true,
  });
}

/** Status, mode and per-entity counts of the selected run. Refetches every 30 s only while the run is still running (and the tab is visible). */
export function useRunMeta(runId: string) {
  const visible = useTabVisible();
  return useQuery({
    ...pushRunOptions(runId),
    refetchOnWindowFocus: true,
    refetchInterval: (q) => (visible && q.state.data?.status === "running" ? SLOW_POLL_MS : false),
  });
}

type Filters = Pick<EntryParams, "outcomes" | "entity" | "code" | "q">;

const entriesQuery = (runId: string, f: Filters, limit: number, offset: number) => ({
  queryKey: keys.entries(runId, { ...f, limit, offset }),
  queryFn: (): Promise<EntriesOut> =>
    unwrap(
      api.GET("/api/v1/push/runs/{run_id}/entries", {
        params: { path: { run_id: runId }, query: { outcome: [...f.outcomes], entity: f.entity || undefined, code: f.code || undefined, q: f.q || undefined, limit, offset } },
      }),
    ),
});

/** One page of entries. Filter and page changes keep the previous rows on screen until the new ones arrive. */
export function useEntries(runId: string, p: EntryParams, running: boolean) {
  const visible = useTabVisible();
  return useQuery({
    ...entriesQuery(runId, p, PAGE_SIZE, (p.page - 1) * PAGE_SIZE),
    placeholderData: (previous, previousQuery) => (previousQuery?.queryKey[1] === runId ? keepPreviousData(previous) : undefined),
    refetchOnWindowFocus: true,
    refetchInterval: visible && running ? SLOW_POLL_MS : false,
  });
}

/**
 * The entry a `?entry=` link points at. It is normally on the loaded page; a deep link may point elsewhere in the run,
 * so then it is looked up by its key (every outcome, any page).
 */
export function useEntry(runId: string, id: string | null, loaded: EntryOut[] | undefined): { entry: EntryOut | undefined; loading: boolean } {
  const onPage = loaded?.find((e) => entryId(e) === id);
  const parts = id ? parseEntryId(id) : null;
  const q = useQuery({
    ...entriesQuery(runId, { outcomes: OUTCOMES, entity: parts?.entity ?? "", code: parts?.code ?? "", q: parts?.key ?? "" }, 200, 0),
    enabled: Boolean(loaded && id && !onPage && parts),
  });
  return { entry: onPage ?? q.data?.items.find((e) => entryId(e) === id), loading: !loaded || q.isFetching };
}
