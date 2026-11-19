import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { openOperations } from "../../test/operations";
import { renderApp } from "../../test/render";
import { http, HttpResponse, liveData, server, signedIn } from "../../test/server";

const SECRET = "https://hooks.example.test/services/T000/B000/XXXX";

function open(over: Parameters<typeof signedIn>[0] = {}) {
  server.use(...liveData(), ...signedIn(over));
  return renderApp("/settings");
}

describe("settings", () => {
  it("shows every server setting as plain text, with the read-only note", async () => {
    open({
      environment: "production",
      stocktrim_writes_enabled: true,
      scheduler_enabled: true,
      scheduler_allow_production: false,
      schedule: { enabled: true, every_days: 3, time: "02:00", tz: "America/Toronto", source: "database" },
      sync_max_planned_calls: 2000,
      stocktrim_host: "api.stocktrim.example",
      stocktrim_max_rps: 5,
      read_jobs: ["customers", "vendors", "items"],
      sales_history_years: 3,
      negative_stock_policy: "as_is",
      alert_webhook_configured: true,
      history_retention_days: 90,
      version: "abc1234",
    });
    expect(await screen.findByText(/These are set on the server and cannot be changed here\./)).toBeInTheDocument();
    const dt = (name: string) => screen.getByText(name, { selector: "dt" }).nextElementSibling as HTMLElement;
    expect(dt("Environment")).toHaveTextContent("production");
    expect(dt("StockTrim writes")).toHaveTextContent("Enabled: runs send changes to StockTrim. Set on the server (STOCKTRIM_WRITES_ENABLED).");
    expect(dt("Automatic runs")).toHaveTextContent("Allowed by the server.");
    expect(dt("Production runs allowed")).toHaveTextContent("No");
    expect(dt("Sync schedule")).toHaveTextContent("Every 3 days at 02:00 (America/Toronto), saved in the database");
    expect(dt("Planned-call limit per sync")).toHaveTextContent("2000");
    expect(dt("StockTrim host")).toHaveTextContent("api.stocktrim.example");
    expect(dt("StockTrim requests per second")).toHaveTextContent("5");
    expect(dt("QuickBooks read jobs")).toHaveTextContent("customers, vendors, items");
    expect(dt("Sales history (years)")).toHaveTextContent("3");
    expect(dt("Negative stock policy")).toHaveTextContent("as_is");
    expect(dt("Alert webhook configured")).toHaveTextContent("Yes");
    expect(dt("History kept (days)")).toHaveTextContent("90");
    expect(screen.getByText("abc1234")).toBeInTheDocument();
  });

  it("scheduler off and writes off say so, and a switched-off schedule reads as such", async () => {
    open({ schedule: { enabled: false, every_days: 1, time: "02:00", tz: "UTC", source: "env_default" } });
    expect(await screen.findByText("Switched off on the server.")).toBeInTheDocument();
    expect(screen.getByText(/Off: runs plan the changes but send nothing\./)).toBeInTheDocument();
    expect(screen.getByText(/Switched off, server default from the environment/)).toBeInTheDocument();
  });

  it("never shows a secret: the webhook is only yes/no and the host is a bare hostname", async () => {
    open({ alert_webhook_configured: true, stocktrim_host: "api.stocktrim.example" });
    await screen.findByText("Alert webhook configured");
    expect(document.body.textContent).not.toMatch(/https?:\/\//);
    expect(document.body.textContent).not.toContain(SECRET);
    expect(screen.queryByText(/signature|token/i)).not.toBeInTheDocument();
  });

  it("lists the users with active state and last login", async () => {
    open({
      users: [
        { username: "ada", is_active: true, last_login_at: "2026-11-18T09:15:00Z" },
        { username: "bob", is_active: false, last_login_at: null },
      ],
    });
    const table = await screen.findByRole("table", { name: "Admin users" });
    const rows = within(table).getAllByRole("row");
    expect(rows[1]).toHaveTextContent(/ada\s*Yes\s*18 Nov/);
    expect(rows[2]).toHaveTextContent(/bob\s*No\s*Never/);
  });

  it("the add-user hint names the exact terminal command (no -T)", async () => {
    open({ users: [] });
    expect(await screen.findByText(/docker compose exec app python -m app\.cli users add <username>/)).toBeInTheDocument();
  });

  it("has nothing to edit: no form controls on the page", async () => {
    open();
    await screen.findByText("Environment", { selector: "dt" });
    const main = screen.getByRole("main");
    expect(within(main).queryAllByRole("textbox")).toHaveLength(0);
    expect(within(main).queryAllByRole("switch")).toHaveLength(0);
    expect(within(main).queryAllByRole("combobox")).toHaveLength(0);
    expect(within(main).queryAllByRole("button")).toHaveLength(0);
  });

  it("a failed load offers a retry", async () => {
    server.use(http.get("*/api/v1/settings", () => HttpResponse.json({ detail: "boom" }, { status: 500 })), ...liveData(), ...signedIn());
    renderApp("/settings");
    expect(await screen.findByText("The settings could not be loaded.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });
});

describe("operations page accessibility and motion", () => {
  it("every control has a visible label, the radio group has a legend, and live regions exist", async () => {
    openOperations();
    const group = await screen.findByRole("group", { name: "How often" });
    expect(group.tagName).toBe("FIELDSET");
    expect(screen.getByLabelText("Time of day (24 h)")).toBeInTheDocument();
    expect(screen.getByLabelText("Time zone")).toBeInTheDocument();
    expect(screen.getByRole("switch", { name: /Automatic sync/ })).toBeInTheDocument();
    expect(screen.getAllByRole("status").length).toBeGreaterThanOrEqual(3);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { name: "Need to do more?" })).toBeInTheDocument();
  });

  it("the dry-run and real-push commands list all six steps and say to reuse them", async () => {
    openOperations();
    await screen.findByRole("heading", { name: "Need to do more?" });
    for (const label of ["Plan a push (dry run, sends nothing)", "Run a real push"]) {
      const text = screen.getByLabelText(label).textContent ?? "";
      for (const step of ["suppliers", "products", "bom", "customers", "sales_orders", "purchase_orders"]) expect(text).toContain(`"${step}"`);
    }
    expect(screen.getByText(/Use the same steps for the real run as for the dry-run/)).toBeInTheDocument();
  });

  it("lists the command-line-only actions with copyable commands", async () => {
    openOperations();
    await screen.findByRole("heading", { name: "Need to do more?" });
    expect(screen.getByText(/Real pushes to StockTrim/)).toBeInTheDocument();
    expect(screen.getByLabelText("Run a real push")).toHaveTextContent("python -m app.cli POST /push --json");
    expect(screen.getByLabelText("Cancel a push run")).toHaveTextContent("/push/runs/<run_id>/cancel");
    expect(screen.getByText("backend/README.md")).toBeInTheDocument();
  });

  it("reduced motion: the page's children are in their final state at once", async () => {
    openOperations();
    const heading = await screen.findByRole("heading", { name: "Operations" });
    for (const child of Array.from(heading.parentElement?.parentElement?.children ?? [])) {
      expect((child as HTMLElement).style.visibility).toBe("");
      expect((child as HTMLElement).style.opacity).toBe("");
    }
  });
});
