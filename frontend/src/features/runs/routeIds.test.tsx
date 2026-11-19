import { screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { isUuid } from "../../lib/uuid";
import { runItem } from "../../test/fixtures";
import { renderApp } from "../../test/render";
import { http, HttpResponse, server, signedIn } from "../../test/server";

// There is deliberately no handler for /cycles/:id or /push/runs/:id: the suite's unhandled-request guard fails the test if one is made.
describe("route ids", () => {
  it("isUuid accepts only UUIDs", () => {
    expect(isUuid("11111111-1111-4111-8111-111111111111")).toBe(true);
    for (const bad of ["", "abc", "../../x", "11111111-1111-4111-8111-11111111111", null, undefined]) expect(isUuid(bad)).toBe(false);
  });

  it("a bad cycle id is not found, without an API call", async () => {
    server.use(...signedIn());
    renderApp("/runs/not-a-uuid");
    expect(await screen.findByRole("heading", { name: "Sync cycle not found" })).toBeInTheDocument();
  });

  it("a bad push run id is not found, without an API call", async () => {
    server.use(...signedIn());
    renderApp("/runs/push/..%2Fsecrets");
    expect(await screen.findByRole("heading", { name: "Push run not found" })).toBeInTheDocument();
  });

  it("a bad ?run= on Entries is not found, without loading entries", async () => {
    server.use(http.get("*/api/v1/push/runs", () => HttpResponse.json([runItem()])), ...signedIn());
    renderApp("/entries?run=nope");
    expect(await screen.findByRole("heading", { name: "Run not found" })).toBeInTheDocument();
  });
});
