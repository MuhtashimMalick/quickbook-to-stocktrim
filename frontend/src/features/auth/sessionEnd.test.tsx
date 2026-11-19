import { act, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { setSessionEnd } from "../../api/session";
import { renderApp } from "../../test/render";
import { http, HttpResponse, server, signedIn, token } from "../../test/server";

const MIN = 60_000;

afterEach(() => vi.useRealTimers());

describe("session end", () => {
  it("warns about 5 minutes before the absolute end, then signs out like any expiry", async () => {
    server.use(http.post("*/api/v1/auth/refresh", () => HttpResponse.json(token("t1", new Date(Date.now() + 24 * 60 * MIN).toISOString()))), ...signedIn());
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Overview" });
    expect(screen.queryByText(/Your session ends at/)).not.toBeInTheDocument();

    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    act(() => setSessionEnd(new Date(Date.now() + 10 * MIN).toISOString()));
    expect(screen.queryByText(/Your session ends at/)).not.toBeInTheDocument();

    await act(() => vi.advanceTimersByTimeAsync(5 * MIN + 1000));
    expect(screen.getByText(/^Your session ends at \d\d:\d\d\.$/)).toBeInTheDocument();
    expect(screen.getByText("Sign in again then; nothing is lost.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Overview" })).toBeInTheDocument(); // non-modal: the page stays usable

    await act(() => vi.advanceTimersByTimeAsync(5 * MIN));
    vi.useRealTimers();
    expect(await screen.findByText("You are signed out. Sign in again.")).toBeInTheDocument();
  });

  it("a refresh that reports a new end replaces the schedule (the end never moves on its own)", async () => {
    server.use(...signedIn());
    renderApp("/");
    await screen.findByRole("heading", { level: 1, name: "Overview" });
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "Date"] });
    act(() => setSessionEnd(new Date(Date.now() + 10 * MIN).toISOString()));
    act(() => setSessionEnd(new Date(Date.now() + 60 * MIN).toISOString()));
    await act(() => vi.advanceTimersByTimeAsync(30 * MIN));
    expect(screen.queryByText(/Your session ends at/)).not.toBeInTheDocument();
  });
});
