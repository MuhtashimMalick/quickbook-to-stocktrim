import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { CycleOut, Overview } from "../../api/types";
import { cycle, overview, pushRun, RUN_ID } from "../../test/fixtures";
import { renderApp } from "../../test/render";
import { http, HttpResponse, liveData, server, signedIn } from "../../test/server";

const DAY_MS = 86_400_000;

function setup(o: Overview = overview(), cycles: CycleOut[] = [cycle()], settings: Parameters<typeof signedIn>[0] = {}) {
  server.use(...liveData(o, cycles), ...signedIn(settings)); // first handler wins: ours before the defaults
  return renderApp("/");
}

describe("overview states", () => {
  it("never run: says so, and shows empty states instead of blanks", async () => {
    setup(overview({ last_cycle: null, snapshot: {}, last_qbwc_contact: null, last_qbwc_contact_age_seconds: null }), []);
    expect(await screen.findByText(/^No sync has run yet\./)).toBeInTheDocument();
    expect(screen.getByText("No sync cycle has run yet.")).toBeInTheDocument();
    expect(screen.getByText("Nothing has been read from QuickBooks yet.")).toBeInTheDocument();
    expect(screen.getByText("Never connected")).toBeInTheDocument();
    expect(screen.getByText("No real push has run yet.")).toBeInTheDocument();
    expect(screen.getByText("Idle. Nothing is running.")).toBeInTheDocument();
  });

  it("idle after a good sync: last finish, next run, recent contact, recent cycles link to the run", async () => {
    setup();
    expect(await screen.findByText(/Last sync finished .*06:12.*, all done\. Next run Fri 02:00\./)).toBeInTheDocument();
    expect(screen.getByText("Recent")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "The five most recent sync cycles" });
    expect(within(table).getAllByRole("link")[0]).toHaveAttribute("href", expect.stringMatching(/^\/runs\/1{8}-/));
  });

  it("waiting for the Web Connector: plain sentence with its last contact", async () => {
    setup(
      overview({
        running_cycle: { id: cycle().id, since: new Date(Date.now() - 600_000).toISOString(), phase: "waiting_for_web_connector" },
        last_qbwc_contact_age_seconds: 840,
      }),
    );
    expect(await screen.findByText("Waiting for the Web Connector. It last checked in 14 minutes ago; we can't hurry it.")).toBeInTheDocument();
    expect(screen.getByText("Recent")).toBeInTheDocument(); // 14 minutes is still inside the 15-minute window
    expect(screen.getByText("Waiting")).toBeInTheDocument();
  });

  it("running: phase, per-entity progress from the running push, and a live region that announces the phase", async () => {
    server.use(http.get("*/api/v1/push/runs/:id", () => HttpResponse.json(pushRun({ status: "running", finished_at: null }))));
    setup(overview({ running_push: { id: RUN_ID, since: new Date().toISOString(), phase: "step_started" } }));
    expect(await screen.findByText("A push run is running: Sending a step.")).toBeInTheDocument();
    expect(await screen.findByRole("progressbar", { name: "supplier progress" })).toHaveAttribute("aria-valuenow", "3");
    expect(screen.getByRole("progressbar", { name: "product progress" })).toHaveAttribute("aria-valuemax", "8");
    const live = screen.getAllByRole("status").find((el) => el.textContent === "Now: Sending a step");
    expect(live).toHaveAttribute("aria-live", "polite");
  });

  it("stale snapshot: a table older than the schedule is flagged, a current one is not", async () => {
    setup(
      overview({
        snapshot: {
          items: { synced_at_max: new Date(Date.now() - 5 * DAY_MS).toISOString(), records: 10 },
          vendors: { synced_at_max: new Date().toISOString(), records: 3 },
        },
      }),
    );
    const table = await screen.findByRole("table", { name: "Freshness of each QuickBooks table" });
    expect(within(within(table).getByRole("row", { name: /items/ })).getByText("Stale")).toBeInTheDocument();
    expect(within(within(table).getByRole("row", { name: /vendors/ })).getByText("Current")).toBeInTheDocument();
    expect(within(table).getAllByRole("columnheader")).toHaveLength(4);
  });

  it("overview error: a retry button that works", async () => {
    let fail = true;
    setup();
    server.use(http.get("*/api/v1/overview", () => (fail ? HttpResponse.json({ detail: "boom" }, { status: 500 }) : HttpResponse.json(overview()))));
    expect(await screen.findByText("The overview could not be loaded.")).toBeInTheDocument();
    fail = false;
    await userEvent.setup().click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText(/Last sync finished/)).toBeInTheDocument();
  });

  it("database down: says so first and does not pretend the data is current", async () => {
    setup(overview({ database: false }));
    expect(await screen.findByText(/^The database is not answering/)).toBeInTheDocument();
    expect(screen.getByText("The database is not reachable")).toBeInTheDocument();
  });

  it("scheduler off on the server: the ceiling is explained, with the stored schedule beside it", async () => {
    setup(overview({ scheduler: { enabled: false, running: false, time: "02:00", every_days: 3, tz: "UTC", schedule_enabled: true, source: "database", next_run_at: null } }));
    expect(await screen.findByText(/Automatic runs are switched off on the server\./)).toBeInTheDocument();
    expect(screen.getByText("Off on the server (the ceiling)")).toBeInTheDocument();
    expect(screen.getByText("Set in this app")).toBeInTheDocument();
    expect(screen.getByText(/Every 3 days at/)).toBeInTheDocument();
  });

  it.each([
    [60, "Recent"],
    [20 * 60, "Late"],
    [3 * 3600, "Silent"],
  ])("Web Connector contact %i s ago reads as %s", async (age, word) => {
    setup(overview({ last_qbwc_contact: new Date(Date.now() - age * 1000).toISOString(), last_qbwc_contact_age_seconds: age }));
    expect(await screen.findByText(word)).toBeInTheDocument();
  });

  it("writes off: one plain notice", async () => {
    setup();
    expect(await screen.findByText("Writes are switched off on the server, so runs plan the changes but send nothing.")).toBeInTheDocument();
  });

  it("production with writes on: banner shown, no writes-off notice", async () => {
    setup(overview(), [cycle()], { environment: "production", stocktrim_writes_enabled: true });
    expect(await screen.findByText("PRODUCTION - writes go to the real StockTrim account")).toBeInTheDocument();
    await screen.findByText(/Last sync finished/);
    expect(screen.queryByText(/Writes are switched off/)).not.toBeInTheDocument();
  });

  it("times are shown in the schedule zone with UTC on hover", async () => {
    setup(overview(), [cycle()], { schedule: { enabled: true, every_days: 1, source: "database", time: "02:00", tz: "America/Chicago" } });
    const t = await screen.findAllByText(/19 Nov.*00:00/); // 06:00 UTC is 00:00 in Chicago (CST)
    expect(t[0]).toHaveAttribute("title", expect.stringContaining("06:00:00 UTC"));
  });
});
