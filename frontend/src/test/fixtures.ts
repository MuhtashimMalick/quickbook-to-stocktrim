import type { CycleDetail, CycleOut, EventOut, Overview, PushRunOut, RunListItem } from "../api/types";

/** Typed builders for API payloads (derived from schema.d.ts, so a contract change breaks the tests at compile time). */
export const CYCLE_ID = "11111111-1111-4111-8111-111111111111";
export const RUN_ID = "22222222-2222-4222-8222-222222222222";
export const T0 = "2026-11-19T06:00:00Z";
export const T1 = "2026-11-19T06:12:00Z";

export const cycle = (over: Partial<CycleOut> = {}): CycleOut => ({
  id: CYCLE_ID,
  status: "done",
  reason: null,
  trigger: "schedule",
  started_at: T0,
  finished_at: T1,
  push_run_id: RUN_ID,
  summary: null,
  ...over,
});

export const overview = (over: Partial<Overview> = {}): Overview => ({
  database: true,
  scheduler: { enabled: true, running: true, time: "02:00", every_days: 1, tz: "UTC", schedule_enabled: true, source: "database", next_run_at: "2026-11-20T02:00:00Z" },
  last_cycle: cycle(),
  last_qbwc_contact: new Date(Date.now() - 120_000).toISOString(),
  last_qbwc_contact_age_seconds: 120,
  jobs: {},
  snapshot: {
    items: { synced_at_max: new Date().toISOString(), records: 2346 },
    vendors: { synced_at_max: new Date().toISOString(), records: 382 },
  },
  running_cycle: null,
  running_push: null,
  last_real_push_run: null,
  ...over,
});

export const pushRun = (over: Partial<PushRunOut> = {}): PushRunOut => ({
  run_id: RUN_ID,
  status: "done",
  dry_run: false,
  steps: ["suppliers", "products"],
  started_at: T0,
  updated_at: T1,
  finished_at: T1,
  counts: { supplier: { done: 3, failed: 0, skipped: 0, pending: 0 }, product: { done: 5, failed: 1, skipped: 0, pending: 2 } },
  error_codes: { product: { rejected_400: 1 } },
  eta_seconds: null,
  summary: null,
  samples: [],
  ...over,
});

export const cycleDetail = (over: Partial<CycleDetail> = {}): CycleDetail => ({
  ...cycle(),
  reason_info: null,
  jobs: [],
  push_run: pushRun(),
  wrote_to_stocktrim: true,
  ...over,
});

export const runItem = (over: Partial<RunListItem> = {}): RunListItem => ({
  id: RUN_ID,
  mode: "sync",
  dry_run: false,
  status: "done",
  steps: ["suppliers"],
  counts: { done: 8 },
  started_at: T0,
  finished_at: T1,
  requested_by: "cli",
  target: "test @ api.example.test",
  ...over,
});

let nextEventId = 1;
export const event = (over: Partial<EventOut> = {}): EventOut => ({
  id: nextEventId++,
  at: T0,
  cycle_id: CYCLE_ID,
  run_id: null,
  level: "info",
  code: "cycle_started",
  title: "Cycle started",
  entity: null,
  counts: null,
  actor: "scheduler",
  ...over,
});
