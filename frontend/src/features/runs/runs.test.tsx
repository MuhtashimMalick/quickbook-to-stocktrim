import { renderHook, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";
import type { CycleDetail, CycleOut, EventOut, RunListItem } from "../../api/types";
import { cycle, cycleDetail, CYCLE_ID, event, pushRun, runItem, RUN_ID } from "../../test/fixtures";
import { renderApp } from "../../test/render";
import { http, HttpResponse, server, signedIn, token } from "../../test/server";
import { useEvents } from "./api";

const cycleId = (n: number) => `00000000-0000-4000-8000-${String(n).padStart(12, "0")}`;

/** An /events handler that behaves like the API: filter by cycle/run, ascending, only ids after `after_id`. */
function eventsHandler(all: () => EventOut[], calls: string[] = []) {
  return http.get("*/api/v1/events", ({ request }) => {
    const u = new URL(request.url);
    calls.push(u.search);
    const after = Number(u.searchParams.get("after_id") ?? 0);
    const cid = u.searchParams.get("cycle_id");
    const rid = u.searchParams.get("run_id");
    const items = all().filter((e) => e.id > after && (!cid || e.cycle_id === cid) && (!rid || e.run_id === rid));
    return HttpResponse.json({ items, last_id: items.at(-1)?.id ?? after });
  });
}

function openCycle(detail: CycleDetail, events: EventOut[] = []) {
  server.use(http.get("*/api/v1/cycles/:id", () => HttpResponse.json(detail)), eventsHandler(() => events), ...signedIn());
  return renderApp(`/runs/${detail.id}`);
}

const ABORTED_EVENTS = [
  event({ code: "cycle_started", title: "Cycle started" }),
  event({ code: "reads_queued", title: "QuickBooks reads queued" }),
  event({ code: "waiting_for_web_connector", title: "Waiting for the Web Connector", level: "warning" }),
  event({ code: "cycle_finished", title: "Cycle finished", level: "error" }),
];

describe("cycles list", () => {
  it("pages with the `before` cursor (the id of the last row) until a short page ends the list", async () => {
    const seen: (string | null)[] = [];
    const page1: CycleOut[] = Array.from({ length: 25 }, (_, i) => cycle({ id: cycleId(100 - i), started_at: `2026-11-19T06:${String(59 - i).padStart(2, "0")}:00Z` }));
    const page2: CycleOut[] = [cycle({ id: cycleId(70) }), cycle({ id: cycleId(69) })];
    server.use(
      http.get("*/api/v1/cycles", ({ request }) => {
        const before = new URL(request.url).searchParams.get("before");
        seen.push(before);
        return HttpResponse.json(before === null ? page1 : page2);
      }),
      ...signedIn(),
    );
    renderApp("/runs");
    const table = await screen.findByRole("table", { name: "Sync cycles, newest first" });
    expect(within(table).getAllByRole("row")).toHaveLength(26);
    await userEvent.setup().click(screen.getByRole("button", { name: "Load more" }));
    await waitFor(() => expect(within(table).getAllByRole("row")).toHaveLength(28));
    expect(seen).toEqual([null, cycleId(76)]);
    expect(screen.queryByRole("button", { name: "Load more" })).not.toBeInTheDocument();
  });

  it("has an empty state and tabs for push runs", async () => {
    server.use(http.get("*/api/v1/cycles", () => HttpResponse.json([])), ...signedIn());
    renderApp("/runs");
    expect(await screen.findByText("No sync cycle has run yet.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Sync cycles" })).toHaveAttribute("aria-current", "page");
  });

  it("shows push runs with the dry-run stamp beside the status and who requested them", async () => {
    const runs: RunListItem[] = [runItem({ dry_run: true, mode: "sync", requested_by: "cli", counts: { done: 0, pending: 12 } })];
    server.use(http.get("*/api/v1/push/runs", () => HttpResponse.json(runs)), ...signedIn());
    renderApp("/runs?view=push");
    const table = await screen.findByRole("table", { name: "Push runs, newest first" });
    expect(within(table).getByText("Dry run")).toBeInTheDocument();
    expect(within(table).getByText("cli")).toBeInTheDocument();
    expect(within(table).getByRole("link")).toHaveAttribute("href", `/runs/push/${RUN_ID}`);
  });

  it("an a11y smoke: one h1, scoped headers, and the h1 takes focus after navigating to a run", async () => {
    server.use(
      http.get("*/api/v1/cycles", () => HttpResponse.json([cycle()])),
      http.get("*/api/v1/cycles/:id", () => HttpResponse.json(cycleDetail())),
      eventsHandler(() => []),
      ...signedIn(),
    );
    renderApp("/runs");
    const table = await screen.findByRole("table");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    within(table).getAllByRole("columnheader").forEach((th) => expect(th).toHaveAttribute("scope", "col"));
    await userEvent.setup().click(within(table).getAllByRole("link")[0]!);
    const h1 = await screen.findByRole("heading", { level: 1, name: "Sync cycle" });
    await waitFor(() => expect(h1).toHaveFocus());
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });
});

describe("cycle detail", () => {
  it("builds the phase ledger in order from events (unknown codes tolerated) and lists every event in the log", async () => {
    const events = [...ABORTED_EVENTS, event({ code: "from_the_future", title: "From the future" })].reverse(); // arrives scrambled
    openCycle(cycleDetail({ status: "aborted", reason: "read_timeout", wrote_to_stocktrim: false, push_run: null, push_run_id: null }), events.map((e, i) => ({ ...e, id: i + 1 })));
    const ledger = await screen.findByRole("list", { name: "Phases of this cycle" });
    await waitFor(() => expect(within(ledger).getByText("Waiting for the Web Connector")).toBeInTheDocument());
    const text = within(ledger).getAllByRole("listitem").map((li) => li.textContent ?? "");
    const at = (t: string) => text.findIndex((x) => x.includes(t));
    expect(at("Cycle started")).toBeLessThan(at("QuickBooks reads queued"));
    expect(at("QuickBooks reads queued")).toBeLessThan(at("Waiting for the Web Connector"));
    expect(at("Waiting for the Web Connector")).toBeLessThan(at("QuickBooks reads finished"));
    expect(within(ledger).getByText("QuickBooks reads finished").closest("li")).toHaveTextContent("Stopped here");
    expect(text.join(" ")).not.toContain("From the future");
    const log = screen.getByRole("table", { name: "Run events, newest first" });
    expect(within(log).getByText("From the future")).toBeInTheDocument();
    expect(within(log).getByText("from_the_future")).toBeInTheDocument();
  });

  it("aborted: reason code, catalog explanation and next step, and that nothing was written", async () => {
    openCycle(
      cycleDetail({
        status: "aborted",
        reason: "read_timeout",
        reason_info: { title: "QuickBooks did not answer in time", explanation: "The reads did not finish within the limit.", next_step: "Check the Web Connector on the client's PC." },
        wrote_to_stocktrim: false,
        push_run: null,
        push_run_id: null,
      }),
    );
    const outcome = (await screen.findByRole("heading", { name: "Outcome" })).closest("section") as HTMLElement;
    expect(within(outcome).getByText("read_timeout")).toBeInTheDocument();
    expect(within(outcome).getByText("QuickBooks did not answer in time")).toBeInTheDocument();
    expect(within(outcome).getByText("The reads did not finish within the limit.")).toBeInTheDocument();
    expect(within(outcome).getByText(/Check the Web Connector/)).toBeInTheDocument();
    expect(within(outcome).getByText("Nothing was written to StockTrim.")).toBeInTheDocument();
  });

  it("dry_run_only: checked and planned, nothing written, with the catalog reason", async () => {
    openCycle(
      cycleDetail({
        status: "dry_run_only",
        reason: "writes_disabled",
        reason_info: { title: "Sending is switched off", explanation: "Everything was checked.", next_step: "Enable writes on the server to send." },
        wrote_to_stocktrim: false,
        push_run: pushRun({ dry_run: true }),
      }),
    );
    expect(await screen.findByText("Nothing was written to StockTrim.")).toBeInTheDocument();
    const outcome = screen.getByRole("heading", { name: "Outcome" }).closest("section") as HTMLElement;
    expect(within(outcome).getByText("writes_disabled")).toBeInTheDocument();
    expect(within(outcome).getByText("Sending is switched off")).toBeInTheDocument();
    expect(screen.getAllByText("Dry run").length).toBeGreaterThan(0);
  });

  it("failed after sending: says records WERE written, and offers the resume command", async () => {
    openCycle(cycleDetail({ status: "failed", reason: "push_done_with_failures", wrote_to_stocktrim: true, push_run: pushRun({ status: "done_with_failures" }) }));
    expect(await screen.findByText(/Records were written to StockTrim/)).toBeInTheDocument();
    expect(screen.queryByText("Nothing was written to StockTrim.")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Command to resume the push run")).toHaveTextContent(`/push/runs/${RUN_ID}/resume`);
  });

  it("interrupted: an explanation and the exact CLI command, no resume button", async () => {
    openCycle(cycleDetail({ status: "interrupted", reason: "restart", wrote_to_stocktrim: true, push_run: pushRun({ status: "interrupted" }) }));
    expect(await screen.findByText("This cycle was interrupted")).toBeInTheDocument();
    expect(screen.getByLabelText("Command to resume the push run")).toHaveTextContent("docker compose exec -T app python -m app.cli POST /push/runs/");
    expect(screen.queryByRole("button", { name: /resume/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Copy command" })).toBeInTheDocument();
  });

  it("done: links to the entries of its push run", async () => {
    openCycle(cycleDetail());
    const link = await screen.findByRole("link", { name: "View entries" });
    expect(link).toHaveAttribute("href", `/entries?run=${RUN_ID}`);
    expect(screen.getByText(/Records were written to StockTrim/).closest("p")).toHaveTextContent("Records were written to StockTrim: 8 sent.");
  });

  it("running: no outcome panel yet, per-entity progress, and live updates keep the page in its final state with reduced motion", async () => {
    openCycle(cycleDetail({ status: "running", finished_at: null, push_run: pushRun({ status: "running", eta_seconds: 90 }) }), [event({ code: "cycle_started" })]);
    expect(await screen.findByRole("progressbar", { name: "product progress" })).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Outcome" })).not.toBeInTheDocument();
    expect(screen.getByText(/About 1m 30s left/)).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("an unknown cycle (404) shows a not-found page, an invalid id too", async () => {
    server.use(http.get("*/api/v1/cycles/:id", () => HttpResponse.json({ detail: "cycle_not_found" }, { status: 404 })), ...signedIn());
    renderApp(`/runs/${CYCLE_ID}`);
    expect(await screen.findByRole("heading", { level: 1, name: "Sync cycle not found" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Back to Runs" })).toHaveAttribute("href", "/runs");
  });

  it("filters the event log by level", async () => {
    openCycle(cycleDetail({ status: "aborted", reason: "read_timeout", push_run: null, push_run_id: null, wrote_to_stocktrim: false }), ABORTED_EVENTS.map((e, i) => ({ ...e, id: i + 1 })));
    const user = userEvent.setup();
    const log = await screen.findByRole("table", { name: "Run events, newest first" });
    expect(within(log).getAllByRole("row")).toHaveLength(5);
    await user.click(screen.getByRole("button", { name: "Warning" }));
    expect(within(screen.getByRole("table", { name: "Run events, newest first" })).getAllByRole("row")).toHaveLength(2);
    await user.click(screen.getByRole("button", { name: "Error" }));
    expect(within(screen.getByRole("table", { name: "Run events, newest first" })).getByText("Cycle finished")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Error" })).toHaveAttribute("aria-pressed", "true");
  });
});

describe("push run detail", () => {
  it("shows a run without a cycle: dry-run stamp, steps, progress, and the cancel command while it runs", async () => {
    server.use(
      http.get("*/api/v1/push/runs/:id", () => HttpResponse.json(pushRun({ status: "running", dry_run: false, finished_at: null }))),
      eventsHandler(() => [event({ cycle_id: null, run_id: RUN_ID, code: "run_started", title: "Run started" })]),
      ...signedIn(),
    );
    renderApp(`/runs/push/${RUN_ID}`);
    expect(await screen.findByRole("heading", { level: 1, name: "Push run" })).toBeInTheDocument();
    expect(await screen.findByLabelText("Command to cancel the run")).toHaveTextContent(`/push/runs/${RUN_ID}/cancel`);
    expect(screen.getByRole("list", { name: "Steps of this run" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View entries" })).toHaveAttribute("href", `/entries?run=${RUN_ID}`);
  });

  it("a dry run says nothing was written", async () => {
    server.use(http.get("*/api/v1/push/runs/:id", () => HttpResponse.json(pushRun({ dry_run: true }))), eventsHandler(() => []), ...signedIn());
    renderApp(`/runs/push/${RUN_ID}`);
    expect(await screen.findByText("Nothing was written to StockTrim")).toBeInTheDocument();
  });

  it("the header shows mode, who requested the run and its target", async () => {
    server.use(http.get("*/api/v1/push/runs/:id", () => HttpResponse.json(pushRun({ mode: "sync", requested_by: "scheduler", target: "test @ api.example.test" }))), eventsHandler(() => []), ...signedIn());
    renderApp(`/runs/push/${RUN_ID}`);
    const header = (await screen.findByText("Requested by")).closest("dl");
    expect(header).not.toBeNull();
    expect(within(header as HTMLElement).getByText("scheduler")).toBeInTheDocument();
    expect(within(header as HTMLElement).getByText("test @ api.example.test")).toBeInTheDocument();
    expect(within(header as HTMLElement).getByText("sync")).toBeInTheDocument();
  });

  it("a missing push run is not found", async () => {
    server.use(http.get("*/api/v1/push/runs/:id", () => HttpResponse.json({ detail: "run_not_found" }, { status: 404 })), ...signedIn());
    renderApp(`/runs/push/${RUN_ID}`);
    expect(await screen.findByRole("heading", { level: 1, name: "Push run not found" })).toBeInTheDocument();
  });
});

describe("incremental events", () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;

  it("asks only for ids after the last one seen, appends them and never duplicates", async () => {
    server.use(http.post("*/api/v1/auth/refresh", () => HttpResponse.json(token("t1"))));
    const calls: string[] = [];
    let all = [1, 2, 3].map((id) => event({ id }));
    server.use(eventsHandler(() => all, calls));
    const { result } = renderHook(() => useEvents({ cycle_id: CYCLE_ID }, false), { wrapper });
    await waitFor(() => expect(result.current.data?.items.map((e) => e.id)).toEqual([1, 2, 3]));
    all = [...all, event({ id: 4 }), event({ id: 5 })];
    await result.current.refetch();
    await waitFor(() => expect(result.current.data?.items.map((e) => e.id)).toEqual([1, 2, 3, 4, 5]));
    expect(calls.map((c) => new URLSearchParams(c).get("after_id"))).toEqual(["0", "3"]);
  });

  it("keeps the events it has when a later poll fails (a cursor error), and recovers", async () => {
    let fail = false;
    const all = [event({ id: 1 }), event({ id: 2 })];
    server.use(
      http.get("*/api/v1/events", ({ request }) => {
        if (fail) return HttpResponse.json({ detail: "invalid" }, { status: 422 });
        const after = Number(new URL(request.url).searchParams.get("after_id"));
        const items = all.filter((e) => e.id > after);
        return HttpResponse.json({ items, last_id: items.at(-1)?.id ?? after });
      }),
    );
    const { result } = renderHook(() => useEvents({ cycle_id: "33333333-3333-4333-8333-333333333333" }, false), { wrapper });
    await waitFor(() => expect(result.current.data?.items).toHaveLength(2));
    expect(result.current.isError).toBe(false); // read it once: react-query only re-renders for the fields that were read
    fail = true;
    await result.current.refetch();
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data?.items).toHaveLength(2);
    fail = false;
    await result.current.refetch();
    await waitFor(() => expect(result.current.isError).toBe(false));
    expect(result.current.data?.items).toHaveLength(2);
  });
});
