import { act, render, renderHook, screen } from "@testing-library/react";
import gsap from "gsap";
import { afterEach, describe, expect, it, vi } from "vitest";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { keys } from "../api/queryKeys";
import { FreshnessNote } from "../components/FreshnessNote";
import { useRunAnnouncement } from "../components/LiveRegion";
import { ProgressBar } from "../components/ProgressBar";
import { overview } from "../test/fixtures";
import { mockMotion } from "../test/motion";
import { renderApp } from "../test/render";
import { http, HttpResponse, liveData, server, settings, signedIn } from "../test/server";
import { useConsecutiveFailures } from "./live";
import { POLL_ACTIVE_MS, POLL_IDLE_MS, usePollInterval } from "./polling";

const setHidden = (hidden: boolean) => {
  Object.defineProperty(document, "visibilityState", { configurable: true, get: () => (hidden ? "hidden" : "visible") });
  document.dispatchEvent(new Event("visibilitychange"));
};
afterEach(() => {
  setHidden(false);
  vi.useRealTimers();
});

const query = <T,>(data: T | undefined) => ({ state: { data } });

describe("usePollInterval", () => {
  it("is 2 s while active and 30 s otherwise", () => {
    const { result, rerender } = renderHook(({ active }) => usePollInterval(active), { initialProps: { active: true } });
    expect(result.current(query(undefined))).toBe(POLL_ACTIVE_MS);
    rerender({ active: false });
    expect(result.current(query(undefined))).toBe(POLL_IDLE_MS);
  });

  it("derives activity from the data when given a function", () => {
    const { result } = renderHook(() => usePollInterval<{ running: boolean }>((d) => d?.running === true));
    expect(result.current(query({ running: true }))).toBe(2000);
    expect(result.current(query({ running: false }))).toBe(30_000);
    expect(result.current(query(undefined))).toBe(30_000);
  });

  it("pauses while the tab is hidden and resumes when it is visible again", () => {
    const { result } = renderHook(() => usePollInterval(true));
    act(() => setHidden(true));
    expect(result.current(query(undefined))).toBe(false);
    act(() => setHidden(false));
    expect(result.current(query(undefined))).toBe(2000);
  });
});

describe("overview polling", () => {
  it("switches between 2 s (running) and 30 s (idle) and stops while hidden", async () => {
    let calls = 0;
    let running = true;
    const ov = () => overview(running ? { running_cycle: { id: "11111111-1111-4111-8111-111111111111", since: new Date().toISOString(), phase: "reads_queued" } } : {});
    server.use(http.get("*/api/v1/overview", () => (calls++, HttpResponse.json(ov()))), ...liveData(), ...signedIn());
    vi.useFakeTimers({ shouldAdvanceTime: true }); // before rendering, so the query's interval is a fake timer
    renderApp("/");
    await screen.findByText(/A sync is running: QuickBooks reads queued\./);
    expect(calls).toBe(1);

    await vi.advanceTimersByTimeAsync(POLL_ACTIVE_MS + 100);
    expect(calls).toBe(2); // running: polled after 2 s

    running = false;
    await vi.advanceTimersByTimeAsync(POLL_ACTIVE_MS + 100); // this poll sees "idle"
    expect(calls).toBe(3);
    await vi.advanceTimersByTimeAsync(POLL_ACTIVE_MS + 100);
    expect(calls).toBe(3); // idle: no poll after 2 s
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS);
    expect(calls).toBe(4); // idle: polled after 30 s

    act(() => setHidden(true));
    await vi.advanceTimersByTimeAsync(POLL_IDLE_MS * 3);
    expect(calls).toBe(4); // hidden: nothing
  });
});

describe("stale data after failed polls", () => {
  it("counts consecutive failures and resets on a success", () => {
    const { result, rerender } = renderHook((p: { errorUpdateCount: number; dataUpdatedAt: number }) => useConsecutiveFailures(p), { initialProps: { errorUpdateCount: 0, dataUpdatedAt: 100 } });
    expect(result.current).toBe(0);
    rerender({ errorUpdateCount: 1, dataUpdatedAt: 100 });
    rerender({ errorUpdateCount: 2, dataUpdatedAt: 100 });
    rerender({ errorUpdateCount: 3, dataUpdatedAt: 100 });
    expect(result.current).toBe(3);
    rerender({ errorUpdateCount: 3, dataUpdatedAt: 200 });
    expect(result.current).toBe(0);
  });

  it("shows 'last updated' normally and a live-updates-stopped alert after three failures", () => {
    const client = new QueryClient();
    client.setQueryData(keys.settings, settings());
    const at = Date.UTC(2026, 10, 19, 6, 12, 3);
    const wrap = (failures: number) => (
      <QueryClientProvider client={client}>
        <FreshnessNote updatedAt={at} failures={failures} />
      </QueryClientProvider>
    );
    const { rerender } = render(wrap(2));
    expect(screen.getByText(/Last updated/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    rerender(wrap(3));
    expect(screen.getByRole("alert")).toHaveTextContent("Live updates have stopped");
  });
});

describe("live announcements", () => {
  it("is silent at first, announces a new phase, stays quiet when nothing changes, and announces the end", () => {
    const { result, rerender } = renderHook(({ phase }) => useRunAnnouncement(phase, "The sync has finished"), { initialProps: { phase: null as string | null } });
    expect(result.current).toBe("");
    rerender({ phase: "Reads queued" });
    expect(result.current).toBe("Now: Reads queued");
    rerender({ phase: "Reads queued" });
    expect(result.current).toBe("Now: Reads queued");
    rerender({ phase: "Changes planned" });
    expect(result.current).toBe("Now: Changes planned");
    rerender({ phase: null });
    expect(result.current).toBe("The sync has finished");
  });
});

describe("reduced motion", () => {
  it("renders a progress bar in its final state at once", () => {
    mockMotion("reduce");
    render(<ProgressBar value={3} max={4} label="products" />);
    const fill = screen.getByRole("progressbar", { name: "products" }).firstElementChild as HTMLElement;
    expect(gsap.getProperty(fill, "scaleX")).toBe(0.75);
  });
});
