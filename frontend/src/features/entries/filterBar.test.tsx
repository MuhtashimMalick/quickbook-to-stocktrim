import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { FilterBar } from "./FilterBar";
import { DEFAULT_OUTCOMES, type EntryParams } from "./params";

const params: EntryParams = { run: null, outcomes: DEFAULT_OUTCOMES, entity: "", code: "", q: "", page: 1, entry: null };
const csv = { status: "idle", truncated: false, run: () => Promise.resolve() } as const;
const bar = (onChange: () => void) => (
  <FilterBar params={params} counts={undefined} total={0} entities={[]} reasons={undefined} csv={csv} onChange={onChange} />
);

afterEach(() => vi.useRealTimers());

describe("search debounce", () => {
  it("a parent re-render (a new onChange every time) does not restart the 300 ms wait", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const first = vi.fn();
    const second = vi.fn();
    const view = render(bar(first));
    fireEvent.change(screen.getByLabelText("Search"), { target: { value: "acme" } });
    await act(() => vi.advanceTimersByTimeAsync(200));
    view.rerender(bar(second)); // e.g. a data refresh re-rendered the page with a fresh inline callback
    await act(() => vi.advanceTimersByTimeAsync(150)); // 350 ms after the keystroke
    expect(second).toHaveBeenCalledTimes(1);
    expect(second).toHaveBeenCalledWith({ q: "acme" });
    expect(first).not.toHaveBeenCalled();
  });
});
