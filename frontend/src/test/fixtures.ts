import type { CycleDetail, CycleOut, EntryOut, EventOut, Overview, PreflightReport, PushRunOut, RunListItem, ScheduleOut } from "../api/types";

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
  last_qbwc_auth_refused: null,
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
  mode: "sync",
  requested_by: "scheduler",
  target: "test @ api.example.test",
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

let nextEntry = 1;
export const entry = (over: Partial<EntryOut> = {}): EntryOut => {
  const n = nextEntry++;
  return {
    source: "item",
    outcome: "failed",
    entity: "product",
    action: "create",
    key: `SKU-${String(n).padStart(4, "0")}`,
    ref: `80000${String(n).padStart(3, "0")}-1700000000`,
    qb_label: `Widget ${n}`,
    code: "rejected_400",
    reason: { title: "StockTrim rejected the record", explanation: "StockTrim refused the data we sent.", next_step: "Fix the field in QuickBooks, then push again." },
    http_status: 400,
    fields: ["name"],
    attempts: 2,
    created_at: T0,
    updated_at: T1,
    run_id: RUN_ID,
    ...over,
  };
};

const slot = (utcHour: number, day: number) => {
  const utc = `2026-11-${day}T${String(utcHour).padStart(2, "0")}:00:00Z`;
  return { utc, local: utc };
};

/** GET/PUT /schedule: a stored daily 02:00 UTC schedule with its three next runs. */
export const scheduleOut = (over: Partial<ScheduleOut> = {}): ScheduleOut => ({
  enabled: true,
  every_days: 1,
  time: "02:00",
  tz: "UTC",
  start_date: "2026-11-01",
  updated_at: "2026-11-10T09:30:00Z",
  updated_by: "ada",
  source: "database",
  next_runs: [slot(2, 20), slot(2, 21), slot(2, 22)],
  ceiling_enabled: true,
  allow_production: true,
  last_cycle: null,
  ...over,
});

const counts = (parsed: number) => ({ parsed, ok: parsed - 2, quarantined: 1, parse_errors: 1, warnings: 0 });

/** POST /preflight: an OK report with no findings. */
export const preflightReport = (verdict: Partial<PreflightReport["verdict"]> = {}): PreflightReport => ({
  quickbooks: {
    entities: { products: counts(300), suppliers: counts(40), customers: counts(120) },
    issues: {},
    bom: {
      assemblies: 0, assemblies_with_edges: 0, edges: 0, summed_duplicates: 0, assemblies_with_summed_duplicates: 0, excluded_lines: 0, dropped_edges: 0,
      quarantined_parents: 0, cycle_ancestors: 0, max_depth: 0, depth_histogram: {}, assemblies_without_lines: 0, assemblies_all_lines_excluded: 0, subassemblies_without_bom: 0,
    },
    negative_stock_count: 0,
  },
  stocktrim: { status: "ok" },
  verdict: { ok_to_push: true, blocking: [], warnings: [], ...verdict },
  snapshot: { synced_at_max: {} },
  plan: { planned_calls: { product: 298 }, total_calls: 460, rps: 5, eta_seconds: 92 },
  fingerprint: "f".repeat(64),
});
