/** Every TanStack Query key in one place, so a hook and its invalidation can never drift apart. */
export interface EventScope {
  cycle_id?: string;
  run_id?: string;
}

export const keys = {
  settings: ["settings"] as const,
  schedule: ["schedule"] as const,
  reasons: ["reason-codes"] as const,
  overview: ["overview"] as const,
  recentCycles: (limit: number) => ["cycles", "recent", limit] as const,
  cycleList: ["cycles", "list"] as const,
  cycle: (id: string) => ["cycles", "detail", id] as const,
  runList: ["push-runs", "list"] as const,
  recentRuns: (limit: number) => ["push-runs", "recent", limit] as const,
  entries: (runId: string, filters: unknown) => ["entries", runId, filters] as const,
  run: (id: string) => ["push-runs", "detail", id] as const,
  events: (scope: EventScope) => ["events", scope.cycle_id ?? null, scope.run_id ?? null] as const,
};
