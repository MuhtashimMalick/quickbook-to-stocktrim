import { describe, expect, it } from "vitest";
import { cycleLedger, phaseTitle, runLedger } from "./ledger";
import { event, RUN_ID } from "../../test/fixtures";

const ev = (code: string, title = code, extra: Parameters<typeof event>[0] = {}) => event({ code, title, ...extra });
const state = (steps: ReturnType<typeof cycleLedger>, id: string) => steps.find((s) => s.id === id)?.state;

describe("cycle ledger", () => {
  it("orders the steps by the known sequence, whatever order the events arrive in", () => {
    const events = [ev("dry_run_planned"), ev("cycle_started"), ev("reads_done"), ev("reads_queued"), ev("snapshot_fresh")];
    const ids = cycleLedger(events, "running").map((s) => s.id);
    expect(ids.indexOf("cycle_started")).toBeLessThan(ids.indexOf("reads_queued"));
    expect(ids.indexOf("reads_queued")).toBeLessThan(ids.indexOf("reads_done"));
    expect(ids.indexOf("reads_done")).toBeLessThan(ids.indexOf("snapshot_fresh"));
    expect(ids.indexOf("snapshot_fresh")).toBeLessThan(ids.indexOf("dry_run_planned"));
  });

  it("ticks what happened, marks the next step current, and leaves the rest pending while it runs", () => {
    const steps = cycleLedger([ev("cycle_started"), ev("reads_queued")], "running");
    expect(state(steps, "reads_queued")).toBe("done");
    expect(state(steps, "waiting_for_web_connector")).toBe("current");
    expect(state(steps, "reads_done")).toBe("pending");
  });

  it("ignores unknown event codes without breaking the ledger", () => {
    const steps = cycleLedger([ev("cycle_started"), ev("something_new_in_b9", "Something new")], "running");
    expect(steps.some((s) => s.id === "something_new_in_b9")).toBe(false);
    expect(state(steps, "cycle_started")).toBe("done");
  });

  it("an aborted cycle fails at the first step it never reached and does not pretend the rest ran", () => {
    const steps = cycleLedger([ev("cycle_started"), ev("reads_queued"), ev("waiting_for_web_connector")], "aborted", "QuickBooks did not answer in time");
    expect(state(steps, "reads_done")).toBe("failed");
    expect(steps.find((s) => s.id === "reads_done")?.note).toBe("QuickBooks did not answer in time");
    expect(state(steps, "real_run_started")).toBe("skipped");
    expect(state(steps, "cycle_finished")).toBe("skipped");
  });

  it("a failed safety check shows as the failed step with the event's title", () => {
    const steps = cycleLedger([ev("cycle_started"), ev("reads_done"), ev("snapshot_fresh"), ev("dry_run_planned"), ev("check_failed:mass_vanish", "Too many records disappeared", { level: "error" })], "aborted");
    expect(steps.find((s) => s.id === "safety_checks")).toMatchObject({ state: "failed", title: "Too many records disappeared" });
    expect(state(steps, "real_run_started")).toBe("skipped");
  });

  it("dry_run_only: checks passed, sending was not run", () => {
    const events = ["cycle_started", "reads_queued", "reads_done", "snapshot_fresh", "dry_run_planned", "cycle_finished"].map((c) => ev(c));
    const steps = cycleLedger(events, "dry_run_only", "Sending is switched off");
    expect(state(steps, "safety_checks")).toBe("done");
    expect(steps.find((s) => s.id === "real_run_started")).toMatchObject({ state: "skipped", note: "Sending is switched off" });
  });

  it("adds a row per pushed step under 'Sending started'", () => {
    const events = [
      ev("real_run_started"),
      ev("step_started", "Step started", { run_id: RUN_ID, entity: "supplier" }),
      ev("step_finished", "Step finished", { run_id: RUN_ID, entity: "supplier", counts: { done: 3 } }),
      ev("step_started", "Step started", { run_id: RUN_ID, entity: "product" }),
    ];
    const steps = cycleLedger(events, "running");
    const ids = steps.map((s) => s.id);
    expect(ids.indexOf("step:supplier")).toBe(ids.indexOf("real_run_started") + 1);
    expect(state(steps, "step:supplier")).toBe("done");
    expect(state(steps, "step:product")).toBe("current");
  });
});

describe("run ledger and phase titles", () => {
  it("lists start, steps and end for a push run", () => {
    const steps = runLedger([ev("run_started"), ev("step_started", "s", { entity: "supplier" })], "running");
    expect(steps.map((s) => s.state)).toEqual(["done", "current", "pending"]);
  });

  it("titles known codes and falls back to a readable form for unknown ones", () => {
    expect(phaseTitle("reads_queued")).toBe("QuickBooks reads queued");
    expect(phaseTitle("brand_new_code")).toBe("brand new code");
    expect(phaseTitle(null)).toBe("");
  });
});
