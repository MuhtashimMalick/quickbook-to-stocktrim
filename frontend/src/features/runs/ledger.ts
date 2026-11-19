import type { EventOut } from "../../api/types";

export type StepState = "done" | "current" | "pending" | "failed" | "skipped";

export interface LedgerStep {
  id: string;
  title: string;
  state: StepState;
  at?: string;
  note?: string;
  counts?: Record<string, number> | null;
  nested?: boolean;
}

/** The known cycle phases, in order. Titles are the catalog titles; the API's own title wins once the event has been seen. */
const CYCLE_PHASES = [
  { code: "cycle_started", title: "Cycle started" },
  { code: "reads_queued", title: "QuickBooks reads queued" },
  { code: "waiting_for_web_connector", title: "Waiting for the Web Connector", optional: true },
  { code: "reads_done", title: "QuickBooks reads finished" },
  { code: "snapshot_fresh", title: "QuickBooks data is current" },
  { code: "dry_run_planned", title: "Changes planned" },
  { code: "safety_checks", title: "Safety checks" },
  { code: "real_run_started", title: "Sending started" },
  { code: "cycle_finished", title: "Cycle finished" },
] as const;

/** Titles for phase codes (the overview only has the code of the newest event). */
export function phaseTitle(code: string | null | undefined): string {
  if (!code) return "";
  if (code.startsWith("check_failed")) return "A safety check failed";
  const known = CYCLE_PHASES.find((p) => p.code === code);
  if (known) return known.title;
  const extra: Record<string, string> = { run_started: "Push run started", step_started: "Sending a step", step_finished: "Step finished", run_finished: "Push run finished" };
  return extra[code] ?? code.replaceAll("_", " ");
}

const FAILING_CYCLE = ["aborted", "failed", "interrupted"];

/** Per-entity step rows of a push run, from step_started / step_finished events. */
function stepRows(events: EventOut[], ended: boolean, failedEnd: boolean): LedgerStep[] {
  const rows = new Map<string, LedgerStep>();
  for (const e of events) {
    if (e.code !== "step_started" && e.code !== "step_finished") continue;
    const name = e.entity ?? "step";
    const finished = e.code === "step_finished";
    const row = rows.get(name) ?? { id: `step:${name}`, title: `Step: ${name}`, state: "current" as StepState, at: e.at, nested: true };
    if (finished) Object.assign(row, { state: "done", at: e.at, counts: e.counts });
    rows.set(name, row);
  }
  return [...rows.values()].map((r) => (r.state === "current" && ended ? { ...r, state: failedEnd ? "failed" : "skipped", note: "Did not finish" } : r));
}

/**
 * The vertical ledger of a cycle. Order is fixed (it does not depend on the order events arrive in);
 * unknown event codes are ignored here and still appear in the event log.
 */
export function cycleLedger(events: EventOut[], cycleStatus: string, reasonTitle?: string): LedgerStep[] {
  const seen = new Map<string, EventOut>();
  for (const e of events) if (!seen.has(e.code)) seen.set(e.code, e);
  const checkFailed = events.find((e) => e.code.startsWith("check_failed"));
  const ended = cycleStatus !== "running";
  const failedEnd = FAILING_CYCLE.includes(cycleStatus);
  const lastSeen = CYCLE_PHASES.reduce((n, p, i) => (seen.has(p.code) ? i : n), -1);

  const steps: LedgerStep[] = [];
  CYCLE_PHASES.forEach((p, i) => {
    if (p.code === "safety_checks") {
      const passed = seen.has("real_run_started") || (ended && (cycleStatus === "done" || cycleStatus === "dry_run_only") && seen.has("dry_run_planned"));
      if (checkFailed) steps.push({ id: p.code, title: checkFailed.title, state: "failed", at: checkFailed.at });
      else steps.push({ id: p.code, title: p.title, state: passed ? "done" : "pending" });
      return;
    }
    const e = seen.get(p.code);
    if (e) return void steps.push({ id: p.code, title: e.title || p.title, state: "done", at: e.at, counts: e.counts });
    if ("optional" in p && lastSeen > i) return; // the Web Connector was already waiting-free: nothing to show
    steps.push({ id: p.code, title: p.title, state: "pending" });
  });

  const run = stepRows(events, ended, failedEnd);
  const at = steps.findIndex((s) => s.id === "real_run_started");
  if (run.length) steps.splice(at + 1, 0, ...run);

  const firstOpen = steps.findIndex((s) => s.state === "pending");
  if (!ended) {
    if (firstOpen >= 0 && !steps.some((s) => s.state === "current")) steps[firstOpen] = { ...steps[firstOpen]!, state: "current" };
    return steps;
  }
  const stopped = steps.some((s) => s.state === "failed");
  return steps.map((s, i) => {
    if (s.state !== "pending") return s;
    if (s.id === "real_run_started" && cycleStatus === "dry_run_only") return { ...s, state: "skipped", note: reasonTitle ?? "Not sent" };
    if (failedEnd && !stopped && i === firstOpen) return { ...s, state: "failed", note: reasonTitle ?? "The cycle stopped here" };
    return { ...s, state: "skipped", note: "Not reached" };
  });
}

/** The ledger of a push run on its own (no cycle): started, one row per step, finished. */
export function runLedger(events: EventOut[], runStatus: string): LedgerStep[] {
  const seen = new Map<string, EventOut>();
  for (const e of events) if (!seen.has(e.code)) seen.set(e.code, e);
  const ended = runStatus !== "running";
  const failedEnd = ["failed", "interrupted", "cancelled", "done_with_failures"].includes(runStatus);
  const start = seen.get("run_started");
  const end = seen.get("run_finished");
  const rows = stepRows(events, ended, failedEnd);
  const steps: LedgerStep[] = [
    { id: "run_started", title: start?.title ?? "Run started", state: start ? "done" : "pending", at: start?.at },
    ...rows,
    { id: "run_finished", title: end?.title ?? "Run finished", state: end ? "done" : "pending", at: end?.at, counts: end?.counts },
  ];
  if (!ended) {
    const open = steps.findIndex((s) => s.state === "pending");
    if (open >= 0 && !steps.some((s) => s.state === "current")) steps[open] = { ...steps[open]!, state: "current" };
  } else {
    return steps.map((s) => (s.state === "pending" ? { ...s, state: "skipped", note: "Not reached" } : s));
  }
  return steps;
}
