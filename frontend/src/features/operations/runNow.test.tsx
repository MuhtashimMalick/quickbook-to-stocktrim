import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { cycleDetail, CYCLE_ID, overview, RUN_ID } from "../../test/fixtures";
import { openOperations } from "../../test/operations";
import { http, HttpResponse, server } from "../../test/server";
import { writesSentence } from "./RunNowPanel";

const trigger = () => screen.getByRole("button", { name: "Run sync now" });
const started = () => HttpResponse.json({ cycle_id: CYCLE_ID, status: "running" }, { status: 202 });

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await screen.findByRole("button", { name: "Run sync now" });
  await user.click(trigger());
  return screen.findByRole("dialog", { name: "Run a sync now?" });
}

describe("run sync now", () => {
  it("explains the wait, the last Web Connector contact and the safety checks; writes OFF says nothing is sent", async () => {
    const user = userEvent.setup();
    openOperations({ settings: { stocktrim_writes_enabled: false } });
    const dialog = await openDialog(user);
    expect(dialog).toHaveTextContent("waits for the QuickBooks Web Connector to check in. It last checked in 2 minutes ago.");
    expect(dialog).toHaveTextContent("minutes to hours");
    expect(dialog).toHaveTextContent("Writes are switched off on the server: this run will plan the changes but send nothing.");
    expect(dialog).toHaveTextContent("follows every automatic safety check");
  });

  it("writes ON says changes will be sent to StockTrim", async () => {
    const user = userEvent.setup();
    openOperations({ settings: { stocktrim_writes_enabled: true } });
    const dialog = await openDialog(user);
    expect(dialog).toHaveTextContent("Writes are ENABLED: if every check passes, this run sends changes to the StockTrim test account (api.example.test).");
    expect(writesSentence(true, "production", false, "api.example.test")).toMatch(/only plan/);
    expect(writesSentence(true, "production", true, "api.stocktrim.example")).toBe("Writes are ENABLED: if every check passes, this run sends changes to the StockTrim production account (api.stocktrim.example).");
  });

  it("says so when the Web Connector has never checked in", async () => {
    const user = userEvent.setup();
    openOperations({ overview: overview({ last_qbwc_contact: null, last_qbwc_contact_age_seconds: null }) });
    expect(await openDialog(user)).toHaveTextContent("It has never checked in.");
  });

  it("a 202 navigates to the new run", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/v1/scheduler/run-now", started),
      http.get("*/api/v1/cycles/:id", () => HttpResponse.json(cycleDetail({ status: "running" }))),
      http.get("*/api/v1/events", () => HttpResponse.json({ items: [], last_id: 0 })),
    );
    openOperations();
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Start the sync" }));
    expect(await screen.findByRole("heading", { name: "Sync cycle" })).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent(`/runs/${CYCLE_ID}`);
  });

  it("a 409 cycle_running closes the dialog; once the overview shows the running cycle the button is disabled and links to it", async () => {
    const user = userEvent.setup();
    let posted = false;
    const idle = overview();
    const busy = overview({ running_cycle: { id: CYCLE_ID, since: new Date().toISOString(), phase: "waiting_for_web_connector" } });
    server.use(
      http.post("*/api/v1/scheduler/run-now", () => {
        posted = true;
        return HttpResponse.json({ detail: "cycle_running" }, { status: 409 });
      }),
    );
    openOperations({ overviewNow: () => (posted ? busy : idle) });
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Start the sync" }));
    expect(await screen.findByText("A sync is already running.")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(trigger()).toBeDisabled();
    expect(screen.getByRole("link", { name: "Follow it step by step" })).toHaveAttribute("href", `/runs/${CYCLE_ID}`);
  });

  it("after a 409 with nothing running any more, the button is enabled again", async () => {
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/scheduler/run-now", () => HttpResponse.json({ detail: "cycle_running" }, { status: 409 })));
    openOperations();
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Start the sync" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger()).toBeEnabled();
    expect(screen.queryByText(/already running/)).not.toBeInTheDocument();
  });

  it("a 403 scheduler_disabled explains why it cannot run; the button stays visible but disabled", async () => {
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/scheduler/run-now", () => HttpResponse.json({ detail: "scheduler_disabled" }, { status: 403 })));
    openOperations();
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Start the sync" }));
    expect(await screen.findByText("Not available: runs are switched off on the server.")).toBeInTheDocument();
    expect(trigger()).toBeDisabled();
  });

  it("knows up front when the server has the scheduler off", async () => {
    openOperations({ overview: overview({ scheduler: { ...overview().scheduler, enabled: false } }) });
    expect(await screen.findByText("Not available: runs are switched off on the server.")).toBeInTheDocument();
    expect(trigger()).toBeDisabled();
  });

  it("is disabled while a cycle runs, and links to it", async () => {
    openOperations({ overview: overview({ running_cycle: { id: CYCLE_ID, since: new Date().toISOString(), phase: "waiting_for_web_connector" } }) });
    expect(await screen.findByText("A sync is already running.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Follow it step by step" })).toHaveAttribute("href", `/runs/${CYCLE_ID}`);
    expect(trigger()).toBeDisabled();
  });

  it("is disabled while a push runs, and links to the push run", async () => {
    openOperations({ overview: overview({ running_push: { id: RUN_ID, since: new Date().toISOString(), phase: "step_started" } }) });
    expect(await screen.findByText("A push run is already running.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Follow it step by step" })).toHaveAttribute("href", `/runs/push/${RUN_ID}`);
    expect(trigger()).toBeDisabled();
  });

  it("a network error keeps the dialog open with a retry that works", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(
      http.post("*/api/v1/scheduler/run-now", () => (++calls === 1 ? HttpResponse.error() : started())),
      http.get("*/api/v1/cycles/:id", () => HttpResponse.json(cycleDetail({ status: "running" }))),
      http.get("*/api/v1/events", () => HttpResponse.json({ items: [], last_id: 0 })),
    );
    openOperations();
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Start the sync" }));
    expect(await within(dialog).findByText("The sync did not start")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("heading", { name: "Sync cycle" })).toBeInTheDocument();
    expect(calls).toBe(2);
  });

  it("dialog: focus moves inside, Escape closes it and focus returns to the button", async () => {
    const user = userEvent.setup();
    openOperations();
    const dialog = await openDialog(user);
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await waitFor(() => expect(trigger()).toHaveFocus());
  });
});
