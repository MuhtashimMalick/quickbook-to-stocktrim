import { screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { renderApp } from "../test/render";
import { http, HttpResponse, liveData, me, server, signedIn, token } from "../test/server";

describe("app shell", () => {
  it("renders landmarks, a skip link and one h1", async () => {
    server.use(...signedIn());
    renderApp("/");
    expect(await screen.findByRole("heading", { level: 1, name: "Overview" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("banner")).toBeInTheDocument();
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
    expect(within(screen.getByRole("navigation", { name: "Main" })).getAllByRole("link")).toHaveLength(5);
    expect(screen.getByRole("link", { name: "Skip to main content" })).toHaveAttribute("href", "#main");
  });

  it("shows the production banner and an ENABLED writes chip from /settings", async () => {
    server.use(...signedIn({ environment: "production", stocktrim_writes_enabled: true }));
    renderApp("/");
    expect(await screen.findByText("PRODUCTION - writes go to the real StockTrim account")).toBeInTheDocument();
    expect(screen.getByText("Writes: ENABLED")).toBeInTheDocument();
    expect(screen.getAllByText("(set on the server)").length).toBeGreaterThan(0);
  });

  it("shows a TEST tag, no banner and a DISABLED chip in the test environment", async () => {
    server.use(...signedIn());
    renderApp("/");
    expect(await screen.findByText("Writes: DISABLED (dry-run only)")).toBeInTheDocument();
    expect(screen.getByText("TEST")).toBeInTheDocument();
    expect(screen.queryByText(/PRODUCTION/)).not.toBeInTheDocument();
    expect(await screen.findByText(/Writes are switched off on the server/)).toBeInTheDocument();
  });

  it("shows a 404 page inside the shell", async () => {
    server.use(...signedIn());
    renderApp("/nowhere");
    expect(await screen.findByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
  });
});

describe("environment fails safe", () => {
  it("shows 'Environment: unknown' and no TEST rule when /settings fails", async () => {
    server.use(
      http.post("*/api/v1/auth/refresh", () => HttpResponse.json(token("t1"))),
      http.get("*/api/v1/auth/me", () => HttpResponse.json(me())),
      http.get("*/api/v1/settings", () => HttpResponse.json({ detail: "boom" }, { status: 500 })),
      ...liveData(),
    );
    renderApp("/");
    expect(await screen.findByText("Environment: unknown")).toBeInTheDocument();
    expect(screen.queryByText("TEST")).not.toBeInTheDocument();
    expect(screen.getByRole("banner").className).not.toContain("border-t-accent");
    expect(await screen.findByRole("alert")).toHaveTextContent("The server settings could not be loaded.");
  });

  it("treats anything that is not exactly 'test' as production", async () => {
    server.use(...signedIn({ environment: "staging" as "test" }));
    renderApp("/");
    expect(await screen.findByText("PRODUCTION - writes go to the real StockTrim account")).toBeInTheDocument();
  });
});
