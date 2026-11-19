import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import type { Finding, ReasonOut } from "../../api/types";
import { preflightReport } from "../../test/fixtures";
import { openOperations } from "../../test/operations";
import { delay, http, HttpResponse, server } from "../../test/server";
import { orderFindings } from "./PreflightReportView";

const CATALOG: Record<string, ReasonOut> = {
  stocktrim_not_empty: { title: "StockTrim account is not empty", explanation: "The account already holds data.", next_step: "Use incremental sync." },
  purchase_orders_present: { title: "StockTrim already has purchase orders", explanation: "It holds purchase orders we did not push.", next_step: "Review them in StockTrim." },
};
const f = (code: string, count = 1, detail?: Record<string, number>): Finding => ({ code, count, ...(detail ? { detail } : {}) });
const reply = (body: ReturnType<typeof preflightReport>) => http.post("*/api/v1/preflight", () => HttpResponse.json(body));
const run = async (user: ReturnType<typeof userEvent.setup>) => {
  await user.click(await screen.findByRole("button", { name: "Run pre-flight check" }));
};

describe("pre-flight", () => {
  it("says it is read-only before it runs", async () => {
    openOperations();
    expect(await screen.findByText("Reads the current QuickBooks snapshot and StockTrim and reports what would be sent. Nothing is written.")).toBeInTheDocument();
  });

  it("OK to push: a stamp, no findings, per-entity counts and the plan", async () => {
    const user = userEvent.setup();
    server.use(reply(preflightReport()));
    openOperations();
    await run(user);
    expect(await screen.findByText("OK to push")).toBeInTheDocument();
    expect(screen.getByText(/No findings/)).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "QuickBooks records by entity" });
    expect(within(table).getByRole("row", { name: /Products 300 298 1 1 0/ })).toBeInTheDocument();
    expect(screen.getByText(/at most/)).toHaveTextContent("460 calls, about 1m 32s at 5 requests per second");
  });

  it("Blocked: blocking findings come before warnings, with catalog titles, counts and a fallback for unknown codes", async () => {
    const user = userEvent.setup();
    server.use(
      reply(
        preflightReport({
          ok_to_push: false,
          blocking: [f("stocktrim_not_empty", 3), f("brand_new_code", 1)],
          warnings: [f("purchase_orders_present", 2, { open: 2 })],
        }),
      ),
    );
    openOperations({ catalog: CATALOG });
    await run(user);
    expect(await screen.findByText("Blocked")).toBeInTheDocument();
    const items = within(screen.getByRole("list", { name: "Findings, blocking first" })).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    expect(items[0]).toHaveTextContent("BlockingStockTrim account is not empty");
    expect(items[0]).toHaveTextContent("The account already holds data.");
    expect(items[0]).toHaveTextContent("Count 3");
    expect(items[1]).toHaveTextContent("brand_new_code");
    expect(items[1]).toHaveTextContent("no description yet");
    expect(items[2]).toHaveTextContent("WarningStockTrim already has purchase orders");
    expect(items[2]).toHaveTextContent("open: 2");
  });

  it("orders blocking first even when the API lists warnings with larger counts", () => {
    const rows = orderFindings({ ok_to_push: false, blocking: [f("a", 1)], warnings: [f("b", 99)] });
    expect(rows.map((r) => r.finding.code)).toEqual(["a", "b"]);
  });

  it("keeps long lists collapsed behind 'Show all'", async () => {
    const user = userEvent.setup();
    server.use(reply(preflightReport({ ok_to_push: false, blocking: ["a", "b", "c", "d"].map((c) => f(c)), warnings: ["e", "f", "g"].map((c) => f(c)) })));
    openOperations();
    await run(user);
    const list = await screen.findByRole("list", { name: "Findings, blocking first" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(5);
    const more = screen.getByRole("button", { name: "Show all 7" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    await user.click(more);
    expect(within(list).getAllByRole("listitem")).toHaveLength(7);
    expect(screen.getByRole("button", { name: "Show fewer" })).toHaveAttribute("aria-expanded", "true");
  });

  it("shows only codes and counts: nothing beyond what the report carries", async () => {
    const user = userEvent.setup();
    const report = preflightReport({ ok_to_push: false, blocking: [f("quarantined_records", 4)] });
    report.quickbooks.issues = { quarantined_records: { severity: "blocking", count: 4, samples: [{ ref: "80000001-1700000000", code: "missing_name" }] } };
    server.use(reply(report));
    openOperations();
    await run(user);
    await screen.findByText("Blocked");
    expect(document.body).not.toHaveTextContent("80000001-1700000000");
  });

  it("an unfinished StockTrim check is called out", async () => {
    const user = userEvent.setup();
    const report = preflightReport();
    report.stocktrim = { status: "aborted", error: "stocktrim_unreachable" };
    server.use(reply(report));
    openOperations();
    await run(user);
    expect(await screen.findByText("The StockTrim part of the check did not finish.")).toBeInTheDocument();
    expect(screen.getByText("stocktrim_unreachable")).toBeInTheDocument();
  });

  it("while it runs: the button is disabled and a live region says 'Checking…'", async () => {
    const user = userEvent.setup();
    server.use(
      http.post("*/api/v1/preflight", async () => {
        await delay(150);
        return HttpResponse.json(preflightReport());
      }),
    );
    openOperations();
    await run(user);
    const button = screen.getByRole("button", { name: "Checking…" });
    expect(button).toBeDisabled();
    const live = screen.getByText(/this reads StockTrim/).closest("[aria-live]");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(await screen.findByText("OK to push")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run pre-flight check" })).toBeEnabled();
  });

  it("a 409 is a calm 'already running', not an error", async () => {
    const user = userEvent.setup();
    server.use(http.post("*/api/v1/preflight", () => HttpResponse.json({ detail: "preflight_running" }, { status: 409 })));
    openOperations();
    await run(user);
    expect(await screen.findByText("Another pre-flight check is already running.")).toBeInTheDocument();
    expect(screen.queryByText("The check could not run")).not.toBeInTheDocument();
  });

  it("any other failure offers a retry that works", async () => {
    const user = userEvent.setup();
    let calls = 0;
    server.use(http.post("*/api/v1/preflight", () => (++calls === 1 ? HttpResponse.json({ detail: "boom" }, { status: 500 }) : HttpResponse.json(preflightReport()))));
    openOperations();
    await run(user);
    await user.click(await screen.findByRole("button", { name: "Try again" }));
    expect(await screen.findByText("OK to push")).toBeInTheDocument();
  });
});
