import { act, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { EntriesOut, EntryOut, PushRunOut, ReasonOut, RunListItem } from "../../api/types";
import { entry, pushRun, runItem, RUN_ID } from "../../test/fixtures";
import { mockMotion } from "../../test/motion";
import { renderApp } from "../../test/render";
import { delay, http, HttpResponse, server, signedIn, token, unauthorized } from "../../test/server";
import { DEFAULT_OUTCOMES, entryId, parseEntryId, parseParams, serializeParams } from "./params";

const OLDER_REAL = "33333333-3333-4333-8333-333333333333";
const NEWER_DRY = "44444444-4444-4444-8444-444444444444";
const CATALOG: Record<string, ReasonOut> = {
  rejected_400: { title: "StockTrim rejected the record", explanation: "StockTrim refused the data we sent.", next_step: "Fix the field in QuickBooks, then push again." },
  cancelled: { title: "Run cancelled", explanation: "The run was stopped before this record was sent.", next_step: "Resume the run to send the rest." },
};

interface Setup {
  runs?: RunListItem[];
  entries?: EntryOut[];
  meta?: Partial<PushRunOut>;
  catalog?: Record<string, ReasonOut>;
  entriesStatus?: number;
  entriesDelayMs?: number;
  /** What the API's `entities` facet answers (default: the distinct entities of all entries). */
  entities?: string[];
}

const many = (n: number, over: Partial<EntryOut> = {}): EntryOut[] => Array.from({ length: n }, () => entry(over));

/** Handlers that behave like the API: filter, count per outcome under the other filters, page by offset. */
function setup(path: string, o: Setup = {}) {
  const all = o.entries ?? [];
  const calls: URLSearchParams[] = [];
  const csv: { headers: Headers; search: URLSearchParams }[] = [];
  server.use(
    http.get("*/api/v1/push/runs", () => HttpResponse.json(o.runs ?? [runItem()])),
    http.get("*/api/v1/push/runs/:id/entries.csv", ({ request }) => {
      csv.push({ headers: request.headers, search: new URL(request.url).searchParams });
      return new HttpResponse("outcome,key\r\nfailed,SKU-1\r\n", { headers: { "Content-Type": "text/csv", "Content-Disposition": 'attachment; filename="entries-2222aaaa.csv"' } });
    }),
    http.get("*/api/v1/push/runs/:id/entries", async ({ request }) => {
      const sp = new URL(request.url).searchParams;
      calls.push(sp);
      if (o.entriesDelayMs) await delay(o.entriesDelayMs);
      if (o.entriesStatus) return HttpResponse.json({ detail: "run_not_found" }, { status: o.entriesStatus });
      const [outcomes, entity, code, q, key] = [sp.getAll("outcome"), sp.get("entity"), sp.get("code"), (sp.get("q") ?? "").toLowerCase(), sp.get("key")];
      const base = all.filter(
        (e) => (!entity || e.entity === entity) && (!code || e.code === code) && (!key || e.key === key) && (!q || [e.key, e.ref ?? "", e.qb_label ?? ""].some((s) => s.toLowerCase().includes(q))),
      );
      const counts = { failed: 0, skipped: 0, excluded: 0 };
      base.forEach((e) => counts[e.outcome]++);
      const picked = base.filter((e) => outcomes.length === 0 || outcomes.includes(e.outcome));
      const offset = Number(sp.get("offset") ?? 0);
      const body: EntriesOut = { items: picked.slice(offset, offset + Number(sp.get("limit") ?? 50)), total: picked.length, counts, entities: o.entities ?? [...new Set(all.map((e) => e.entity))] };
      return HttpResponse.json(body);
    }),
    http.get("*/api/v1/push/runs/:id", ({ params }) => HttpResponse.json(pushRun({ run_id: String(params.id), ...o.meta }))),
    http.get("*/api/v1/reason-codes", () => HttpResponse.json(o.catalog ?? CATALOG)),
    ...signedIn(),
  );
  return { calls, csv, ...renderApp(path) };
}

const where = () => screen.getByTestId("location").textContent ?? "";
const search = () => new URLSearchParams(where().split("?")[1] ?? "");
const table = () => screen.findByRole("table", { name: /Entries of the selected run/ });
const rowButton = async (key: string) => within(await table()).getByRole("button", { name: key });

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("URL state", () => {
  it("round-trips outcome, entity, code, q, page and entry between the URL and the controls", () => {
    const sp = new URLSearchParams([["run", RUN_ID], ["outcome", "excluded"], ["entity", "product"], ["code", "rejected_400"], ["q", "abc"], ["page", "3"], ["entry", "item|product|SKU-1|rejected_400"]]);
    const parsed = parseParams(sp);
    expect(parsed).toMatchObject({ run: RUN_ID, outcomes: ["excluded"], entity: "product", code: "rejected_400", q: "abc", page: 3, entry: "item|product|SKU-1|rejected_400" });
    expect(serializeParams(parsed).toString()).toBe(sp.toString());
    expect(serializeParams(parseParams(new URLSearchParams())).toString()).toBe(""); // defaults leave the URL plain
    expect(parseParams(new URLSearchParams("outcome=bogus&page=-4")).outcomes).toEqual(DEFAULT_OUTCOMES);
    expect(parseParams(new URLSearchParams("page=abc")).page).toBe(1);
  });

  it("an entry id is stable and parseable back to what narrows a lookup", () => {
    const e = entry({ key: "a|b", code: null, entity: "vendor" });
    expect(entryId(e)).toBe("item|vendor|a|b|");
    expect(parseEntryId(entryId(e))).toEqual({ entity: "vendor", key: "a|b", code: "" });
  });

  it("opens with the filters from the URL: chips, selects, search text and the page offset", async () => {
    const { calls } = setup(`/entries?run=${RUN_ID}&outcome=excluded&entity=product&code=rejected_400&q=widget&page=2`, {
      entries: many(120, { outcome: "excluded" }),
    });
    await table();
    expect(screen.getByRole("button", { name: /^Excluded/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /^Failed/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByLabelText("Entity")).toHaveValue("product");
    expect(screen.getByLabelText("Reason")).toHaveValue("rejected_400");
    expect(screen.getByLabelText("Search")).toHaveValue("widget");
    const first = calls[0];
    expect(first?.getAll("outcome")).toEqual(["excluded"]);
    expect([first?.get("entity"), first?.get("code"), first?.get("q"), first?.get("offset"), first?.get("limit")]).toEqual(["product", "rejected_400", "widget", "50", "50"]);
  });
});

describe("run selection and outcome defaults", () => {
  it("defaults to the latest REAL run (a newer dry run is skipped) and pins it in the URL", async () => {
    const runs = [runItem({ id: NEWER_DRY, dry_run: true }), runItem({ id: OLDER_REAL }), runItem({ id: RUN_ID })];
    const { calls } = setup("/entries", { runs, entries: [entry()] });
    await table();
    await waitFor(() => expect(search().get("run")).toBe(OLDER_REAL));
    expect(calls).toHaveLength(1);
    const options = within(screen.getByLabelText("Run")).getAllByRole("option");
    expect(options[0]).toHaveTextContent(/dry run/); // dry runs are labelled in the picker
    expect(options).toHaveLength(3);
  });

  it("falls back to the latest run when every recent run is a dry run, and shows its dry-run stamp", async () => {
    setup("/entries", { runs: [runItem({ id: NEWER_DRY, dry_run: true })], meta: { dry_run: true }, entries: [entry()] });
    await table();
    await waitFor(() => expect(search().get("run")).toBe(NEWER_DRY));
    expect(await screen.findByText("Dry run", { selector: "span" })).toBeInTheDocument();
  });

  it("switching runs resets the narrowing filters and page but keeps the outcomes", async () => {
    const user = userEvent.setup();
    setup(`/entries?run=${RUN_ID}&outcome=excluded&entity=product&page=2`, { runs: [runItem(), runItem({ id: OLDER_REAL })], entries: many(80, { outcome: "excluded" }) });
    await table();
    await user.selectOptions(screen.getByLabelText("Run"), OLDER_REAL);
    await waitFor(() => expect(search().get("run")).toBe(OLDER_REAL));
    expect(search().getAll("outcome")).toEqual(["excluded"]);
    expect(search().has("entity")).toBe(false);
    expect(search().has("page")).toBe(false);
  });

  it("requests failed + skipped by default and shows a live count on every chip", async () => {
    const entries = [...many(3, { outcome: "failed" }), ...many(2, { outcome: "skipped", code: "cancelled" }), ...many(4, { outcome: "excluded" })];
    const { calls } = setup(`/entries?run=${RUN_ID}`, { entries });
    await table();
    expect(calls[0]?.getAll("outcome")).toEqual(["failed", "skipped"]);
    expect(screen.getByRole("button", { name: /^Failed 3$/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /^Skipped 2$/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /^Excluded 4$/ })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByText(/^Showing/)).toHaveTextContent("Showing 1-5 of 5");
  });

  it("toggling chips writes the outcomes to the URL, and the last chip cannot be switched off", async () => {
    const user = userEvent.setup();
    setup(`/entries?run=${RUN_ID}`, { entries: [...many(2, { outcome: "failed" }), ...many(1, { outcome: "excluded" })] });
    await table();
    await user.click(screen.getByRole("button", { name: /^Excluded/ }));
    await waitFor(() => expect(search().getAll("outcome")).toEqual(["failed", "skipped", "excluded"]));
    await user.click(screen.getByRole("button", { name: /^Failed/ }));
    await user.click(screen.getByRole("button", { name: /^Skipped/ }));
    await waitFor(() => expect(search().getAll("outcome")).toEqual(["excluded"]));
    await user.click(screen.getByRole("button", { name: /^Excluded/ })); // the only one left: stays on
    expect(screen.getByRole("button", { name: /^Excluded/ })).toHaveAttribute("aria-pressed", "true");
    await user.click(screen.getByRole("button", { name: /^Failed/ })); // back to the default pair: the URL is plain again
    await user.click(screen.getByRole("button", { name: /^Skipped/ }));
    await user.click(screen.getByRole("button", { name: /^Excluded/ }));
    await waitFor(() => expect(search().has("outcome")).toBe(false));
  });
});

describe("filters", () => {
  it("entity and reason selects use catalog titles, filter the request and reset the page", async () => {
    const user = userEvent.setup();
    const { calls } = setup(`/entries?run=${RUN_ID}&page=2`, { entries: [...many(60, { entity: "product" }), ...many(2, { entity: "supplier", code: "cancelled", outcome: "skipped" })] });
    await table();
    await user.selectOptions(screen.getByLabelText("Reason"), "Run cancelled");
    await waitFor(() => expect(search().get("code")).toBe("cancelled"));
    expect(search().has("page")).toBe(false);
    await user.selectOptions(screen.getByLabelText("Entity"), "supplier");
    await waitFor(() => expect(search().get("entity")).toBe("supplier"));
    await waitFor(() => expect(calls.at(-1)?.get("entity")).toBe("supplier"));
    expect(calls.at(-1)?.get("code")).toBe("cancelled");
  });

  it("the entity dropdown comes from the response's entities: an exclusion-only entity is selectable, and the selected one stays", async () => {
    const user = userEvent.setup();
    setup(`/entries?run=${RUN_ID}`, { entries: many(2, { entity: "product" }), entities: ["customer", "product"] });
    await table();
    const options = () => within(screen.getByLabelText("Entity")).getAllByRole("option").map((o) => o.textContent);
    expect(options()).toEqual(["All entities", "customer", "product"]);
    await user.selectOptions(screen.getByLabelText("Entity"), "customer");
    await waitFor(() => expect(search().get("entity")).toBe("customer"));
    expect(options()).toContain("customer");
  });

  it("search is debounced to about 300 ms: one URL change and one request per pause, not per keystroke", async () => {
    const { calls } = setup(`/entries?run=${RUN_ID}`, { entries: [entry({ key: "ACME-1", qb_label: "Acme Ltd" })] });
    await table();
    const before = calls.length;
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const box = screen.getByLabelText("Search");
    fireEvent.change(box, { target: { value: "a" } });
    await act(() => vi.advanceTimersByTimeAsync(200));
    fireEvent.change(box, { target: { value: "ac" } });
    await act(() => vi.advanceTimersByTimeAsync(200));
    fireEvent.change(box, { target: { value: "acm" } });
    await act(() => vi.advanceTimersByTimeAsync(299));
    expect(search().has("q")).toBe(false);
    await act(() => vi.advanceTimersByTimeAsync(2));
    expect(search().get("q")).toBe("acm");
    vi.useRealTimers();
    await waitFor(() => expect(calls.length).toBe(before + 1));
    expect(calls.at(-1)?.get("q")).toBe("acm");
    expect(screen.getByLabelText("Search")).toHaveAccessibleDescription(/can take a few seconds on very large runs/);
    expect(screen.getByPlaceholderText("QuickBooks name, ListID or TxnID")).toBeInTheDocument();
  });
});

describe("pagination", () => {
  it("shows 'Showing 51-100 of 120', pages by offset and disables the buttons at the boundaries", async () => {
    const user = userEvent.setup();
    const { calls } = setup(`/entries?run=${RUN_ID}`, { entries: many(120) });
    await table();
    expect(screen.getByText(/^Showing/)).toHaveTextContent("Showing 1-50 of 120");
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(screen.getByText(/^Showing/)).toHaveTextContent("Showing 51-100 of 120"));
    expect(search().get("page")).toBe("2");
    expect(calls.at(-1)?.get("offset")).toBe("50");
    await user.click(screen.getByRole("button", { name: "Next page" }));
    await waitFor(() => expect(screen.getByText(/^Showing/)).toHaveTextContent("Showing 101-120 of 120"));
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Previous page" }));
    await waitFor(() => expect(search().get("page")).toBe("2"));
  });

  it("a page past the end steps back to the last page that exists", async () => {
    setup(`/entries?run=${RUN_ID}&page=9`, { entries: many(60) });
    await waitFor(() => expect(search().get("page")).toBe("2"));
    await waitFor(() => expect(screen.getByText(/^Showing/)).toHaveTextContent("Showing 51-60 of 60"));
  });

  it("has no pager for a single page", async () => {
    setup(`/entries?run=${RUN_ID}`, { entries: many(3) });
    await table();
    expect(screen.queryByRole("navigation", { name: "Pages" })).not.toBeInTheDocument();
  });
});

describe("states", () => {
  it("loading: ruled skeleton rows with a status for assistive tech", async () => {
    setup(`/entries?run=${RUN_ID}`, { entries: [entry()], entriesDelayMs: 60 });
    expect(await screen.findByText("Loading the entries…")).toBeInTheDocument();
    await table();
  });

  it("error: a message and a retry that works", async () => {
    const user = userEvent.setup();
    let fail = true;
    setup(`/entries?run=${RUN_ID}`, { entries: [entry()] });
    server.use(http.get("*/api/v1/push/runs/:id/entries", () => (fail ? HttpResponse.json({ detail: "boom" }, { status: 500 }) : HttpResponse.json({ items: [entry({ key: "OK-1" })], total: 1, counts: { failed: 1, skipped: 0, excluded: 0 } }))));
    expect(await screen.findByText("The entries could not be loaded.")).toBeInTheDocument();
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await rowButton("OK-1")).toBeInTheDocument();
  });

  it("nothing to review: only when the run truly has no failed, skipped or excluded entries", async () => {
    setup(`/entries?run=${RUN_ID}`, { entries: [] });
    expect(await screen.findByText("Nothing needs attention.")).toBeInTheDocument();
    expect(screen.getByText("Every entry in this run was sent.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Clear filters" })).not.toBeInTheDocument();
  });

  it("nothing failed or skipped but some excluded: says so and offers to show them", async () => {
    const user = userEvent.setup();
    setup(`/entries?run=${RUN_ID}`, { entries: many(4, { outcome: "excluded" }) });
    expect(await screen.findByText("No failed or skipped entries in this run.")).toBeInTheDocument();
    expect(screen.getByText("4 records were left out (excluded) before sending.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Show excluded entries" }));
    expect(await table()).toBeInTheDocument();
    expect(search().getAll("outcome")).toEqual(["failed", "skipped", "excluded"]);
  });

  it("no matches: says so and Clear filters restores the default view", async () => {
    const user = userEvent.setup();
    setup(`/entries?run=${RUN_ID}&q=zzz&entity=product&outcome=failed`, { entries: many(3) });
    expect(await screen.findByText("No entries match these filters.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(await table()).toBeInTheDocument();
    expect(where()).toBe(`/entries?run=${RUN_ID}`);
    expect(screen.getByLabelText("Search")).toHaveValue("");
  });

  it("no real run yet: 'No sync has run yet' with a link to Runs", async () => {
    setup("/entries", { runs: [] });
    expect(await screen.findByText("No sync has run yet")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to Runs" })).toHaveAttribute("href", "/runs");
  });

  it.each([404, 422])("a run the API does not know (%i) shows the not-found page", async (status) => {
    setup(`/entries?run=${OLDER_REAL}`, { entriesStatus: status });
    expect(await screen.findByRole("heading", { level: 1, name: "Run not found" })).toBeInTheDocument();
  });
});

describe("table and catalog", () => {
  it("has scoped headers, the key in mono with the QuickBooks name below it, and a reason title from the catalog", async () => {
    setup(`/entries?run=${RUN_ID}`, { entries: [entry({ key: "SKU-7", qb_label: "Blue widget", reason: { title: "From the entry", explanation: "e", next_step: "n" } })] });
    const t = await table();
    expect(within(t).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Outcome", "Entity", "Identifier", "Reason", "Attempts", "Last attempt", "Run"]);
    expect(within(t).getAllByRole("columnheader").every((h) => h.getAttribute("scope") === "col")).toBe(true);
    const row = within(t).getAllByRole("row")[1];
    expect(row).toBeDefined();
    expect(within(row as HTMLElement).getByText("Failed")).toBeInTheDocument(); // stamp: icon + word
    expect(within(row as HTMLElement).getByText("SKU-7").closest("button")).toHaveClass("font-mono");
    expect(within(row as HTMLElement).getByText("Blue widget")).toBeInTheDocument();
    expect(within(row as HTMLElement).getByText("StockTrim rejected the record")).toHaveAttribute("title", "rejected_400"); // catalog wins; code on hover
    expect(within(row as HTMLElement).getByRole("link", { name: RUN_ID.slice(0, 8) })).toHaveAttribute("href", `/runs/push/${RUN_ID}`);
  });

  it("an unknown reason code falls back to the explanation sent with the entry, and with none to the bare code", async () => {
    setup(`/entries?run=${RUN_ID}`, {
      entries: [
        entry({ key: "A-1", code: "brand_new_code", reason: { title: "Something unexpected", explanation: "x", next_step: "y" } }),
        entry({ key: "B-1", code: "other_new_code", reason: { title: "", explanation: "", next_step: "" } }),
        entry({ key: "C-1", code: null, reason: { title: "", explanation: "", next_step: "" } }),
      ],
    });
    const t = await table();
    expect(within(t).getByText("Something unexpected")).toHaveAttribute("title", "brand_new_code");
    expect(within(t).getByText("other_new_code")).toBeInTheDocument();
    expect(within(t).getByText("No reason recorded")).toBeInTheDocument();
  });
});

describe("detail drawer", () => {
  const three = () => [entry({ key: "K-1", qb_label: "First" }), entry({ key: "K-2", qb_label: "Second", fields: ["name", "sku"], http_status: 422 }), entry({ key: "K-3", qb_label: null, ref: null, http_status: null, fields: [], attempts: null })];

  it("opens from a row, labelled by its reason, with identifiers, plain explanation and next step; Esc closes and returns focus to the row", async () => {
    const user = userEvent.setup();
    setup(`/entries?run=${RUN_ID}`, { entries: three() });
    const button = await rowButton("K-2");
    await user.click(button);
    const dialog = await screen.findByRole("dialog", { name: "StockTrim rejected the record" });
    expect(dialog).toHaveAccessibleDescription("product: K-2");
    expect(search().get("entry")).toBe("item|product|K-2|rejected_400");
    expect(within(dialog).getByText("StockTrim refused the data we sent.")).toBeInTheDocument();
    expect(within(dialog).getByText("Fix the field in QuickBooks, then push again.")).toBeInTheDocument();
    expect(within(dialog).getByText("What to do next")).toBeInTheDocument();
    expect(within(dialog).getByText("name, sku")).toBeInTheDocument(); // rejected field names
    expect(within(dialog).getByText("422")).toBeInTheDocument();
    expect(within(dialog).getByText("Second")).toBeInTheDocument();
    expect(within(dialog).getByRole("link", { name: "Open this run" })).toHaveAttribute("href", `/runs/push/${RUN_ID}`);
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(search().has("entry")).toBe(false);
    expect(await rowButton("K-2")).toHaveFocus();
  });

  it("previous / next walk the loaded page, and focus goes back to the row last viewed", async () => {
    const user = userEvent.setup();
    setup(`/entries?run=${RUN_ID}`, { entries: three() });
    await user.click(await rowButton("K-1"));
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("button", { name: "Previous entry" })).toBeDisabled();
    await user.click(within(dialog).getByRole("button", { name: "Next entry" }));
    await waitFor(() => expect(search().get("entry")).toBe("item|product|K-2|rejected_400"));
    expect(within(within(dialog).getByRole("navigation", { name: "Entries on this page" })).getByText("2")).toBeInTheDocument();
    await user.click(within(dialog).getByRole("button", { name: "Next entry" }));
    await waitFor(() => expect(search().get("entry")).toBe("item|product|K-3|rejected_400"));
    expect(within(dialog).getByRole("button", { name: "Next entry" })).toBeDisabled();
    expect(within(dialog).queryByText("QuickBooks ID")).not.toBeInTheDocument(); // K-3 has no QuickBooks ref
    await user.click(within(dialog).getByRole("button", { name: "Previous entry" }));
    await waitFor(() => expect(search().get("entry")).toBe("item|product|K-2|rejected_400"));
    await user.click(within(dialog).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(await rowButton("K-2")).toHaveFocus();
  });

  it("a deep link opens the drawer on load, including an entry that is not on the loaded page", async () => {
    const entries = many(70);
    const far = entries[65];
    expect(far).toBeDefined();
    const { calls } = setup(`/entries?run=${RUN_ID}&entry=${encodeURIComponent(entryId(far as EntryOut))}`, { entries });
    const dialog = await screen.findByRole("dialog", { name: "StockTrim rejected the record" });
    expect(await within(dialog).findByText((far as EntryOut).key, { selector: "bdi" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Next entry" })).not.toBeInTheDocument(); // not on the page: no previous / next
    const lookup = calls.find((c) => c.get("key") === (far as EntryOut).key);
    expect(lookup?.has("q")).toBe(false); // the exact key filter, never the substring search
  });

  it("a deep link whose entries list failed shows an error, not a loading state forever", async () => {
    setup(`/entries?run=${RUN_ID}&entry=${encodeURIComponent("item|product|SKU-1|rejected_400")}`, { entries: many(2), entriesStatus: 500 });
    const dialog = await screen.findByRole("dialog", { name: "The entry could not be loaded" });
    expect(within(dialog).getByRole("alert")).toBeInTheDocument();
    expect(within(dialog).queryByRole("status")).not.toBeInTheDocument();
  });

  it("closing a drawer opened from a deep link to an entry that is not on this page puts focus on the page heading", async () => {
    const user = userEvent.setup();
    const entries = many(70);
    setup(`/entries?run=${RUN_ID}&page=2&entry=${encodeURIComponent(entryId(entries[3] as EntryOut))}`, { entries });
    await screen.findByRole("dialog", { name: "StockTrim rejected the record" });
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByRole("heading", { level: 1 })).toHaveFocus());
  });

  it("an entry that no longer exists says so instead of showing an empty panel", async () => {
    setup(`/entries?run=${RUN_ID}&entry=${encodeURIComponent("item|product|GONE|rejected_400")}`, { entries: many(2) });
    expect(await screen.findByRole("dialog", { name: "Entry not found" })).toBeInTheDocument();
  });

  it("copy buttons copy the identifier and the QuickBooks ID and announce it", async () => {
    const user = userEvent.setup();
    const e = entry({ key: "SKU-COPY", ref: "80000ABC-1700000000" });
    setup(`/entries?run=${RUN_ID}`, { entries: [e] });
    await user.click(await rowButton("SKU-COPY"));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Copy identifier" }));
    expect(await navigator.clipboard.readText()).toBe("SKU-COPY");
    await waitFor(() => expect(within(dialog).getAllByRole("status").some((s) => s.textContent === "Copied")).toBe(true));
    await user.click(within(dialog).getByRole("button", { name: "Copy QuickBooks ID" }));
    expect(await navigator.clipboard.readText()).toBe("80000ABC-1700000000");
  });

  it("keeps focus inside the dialog while tabbing (focus trap)", async () => {
    const user = userEvent.setup();
    setup(`/entries?run=${RUN_ID}`, { entries: three() });
    await user.click(await rowButton("K-1"));
    const dialog = await screen.findByRole("dialog");
    for (let i = 0; i < 12; i++) {
      await user.tab();
      expect(dialog.contains(document.activeElement)).toBe(true);
    }
  });

  it("reduced motion: closes at once; full motion: slides out (0.15 s) and then closes", async () => {
    const user = userEvent.setup();
    setup(`/entries?run=${RUN_ID}`, { entries: three() });
    mockMotion("reduce");
    await user.click(await rowButton("K-1"));
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument(), { timeout: 100 });

    mockMotion("no-preference");
    await user.click(await rowButton("K-1"));
    await screen.findByRole("dialog");
    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(search().has("entry")).toBe(false);
  });
});

describe("CSV export", () => {
  function stubDownload() {
    const created: Blob[] = [];
    const revoked: string[] = [];
    const downloads: string[] = [];
    Object.assign(URL, {
      createObjectURL: (b: Blob) => (created.push(b), `blob:test/${created.length}`),
      revokeObjectURL: (u: string) => void revoked.push(u),
    });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (this: HTMLAnchorElement) {
      downloads.push(this.download);
    });
    return { created, revoked, downloads };
  }

  it("puts the row count in the label when small, then downloads through the authenticated client with the same filters", async () => {
    const user = userEvent.setup();
    const dl = stubDownload();
    const { csv } = setup(`/entries?run=${RUN_ID}&entity=product&q=widget`, { entries: many(3) });
    const button = await screen.findByRole("button", { name: "Export CSV (3 rows)" });
    await user.click(button);
    await waitFor(() => expect(dl.downloads).toEqual(["entries-2222aaaa.csv"])); // the file name comes from Content-Disposition
    expect(csv).toHaveLength(1);
    expect(csv[0]?.headers.get("Authorization")).toMatch(/^Bearer .+/);
    expect(csv[0]?.search.getAll("outcome")).toEqual(["failed", "skipped"]);
    expect([csv[0]?.search.get("entity"), csv[0]?.search.get("q")]).toEqual(["product", "widget"]);
    expect(dl.created).toHaveLength(1);
    await waitFor(() => expect(dl.revoked).toEqual(["blob:test/1"]), { timeout: 3000 }); // revoked after about a second, not at once
    expect(screen.queryByText(/The export was cut/)).not.toBeInTheDocument();
    expect(await screen.findByRole("button", { name: "Export CSV (3 rows)" })).toBeEnabled();
  });

  it("shows 'Preparing export...' with the button disabled while the file is made", async () => {
    const user = userEvent.setup();
    stubDownload();
    setup(`/entries?run=${RUN_ID}`, { entries: many(2) });
    server.use(
      http.get("*/api/v1/push/runs/:id/entries.csv", async () => {
        await delay(120);
        return new HttpResponse("x", { headers: { "Content-Disposition": 'attachment; filename="a.csv"' } });
      }),
    );
    await user.click(await screen.findByRole("button", { name: /^Export CSV/ }));
    const busy = await screen.findByRole("button", { name: "Preparing export..." });
    expect(busy).toBeDisabled();
    expect(screen.getAllByText("Preparing export...").some((el) => el.getAttribute("aria-live") === "polite")).toBe(true);
    expect(await screen.findByRole("button", { name: /^Export CSV/ })).toBeEnabled();
  });

  it("a 401 token_expired on the download refreshes the session and retries it once", async () => {
    const user = userEvent.setup();
    const dl = stubDownload();
    setup(`/entries?run=${RUN_ID}`, { entries: many(2) });
    const seen: (string | null)[] = [];
    let refreshes = 0;
    server.use(
      http.post("*/api/v1/auth/refresh", () => HttpResponse.json(token(`fresh-${++refreshes}`))),
      http.get("*/api/v1/push/runs/:id/entries.csv", ({ request }) => {
        seen.push(request.headers.get("Authorization"));
        return seen.length === 1 ? unauthorized("token_expired") : new HttpResponse("x", { headers: { "Content-Disposition": 'attachment; filename="r.csv"' } });
      }),
    );
    await user.click(await screen.findByRole("button", { name: /^Export CSV/ }));
    await waitFor(() => expect(dl.downloads).toEqual(["r.csv"]));
    expect(seen).toHaveLength(2);
    expect(seen[1]).toMatch(/^Bearer fresh-/);
    expect(screen.queryByText("The export failed")).not.toBeInTheDocument();
  });

  it("the cut notice goes away when the filters change", async () => {
    const user = userEvent.setup();
    stubDownload();
    setup(`/entries?run=${RUN_ID}`, { entries: many(2) });
    server.use(http.get("*/api/v1/push/runs/:id/entries.csv", () => new HttpResponse("x", { headers: { "Content-Disposition": 'attachment; filename="a.csv"', "X-Truncated": "true" } })));
    await user.click(await screen.findByRole("button", { name: /^Export CSV/ }));
    await screen.findByText(/The export was cut/);
    await user.click(screen.getByRole("button", { name: /^Skipped/ }));
    await waitFor(() => expect(screen.queryByText(/The export was cut/)).not.toBeInTheDocument());
  });

  it("X-Truncated: true shows the cut notice", async () => {
    const user = userEvent.setup();
    stubDownload();
    setup(`/entries?run=${RUN_ID}`, { entries: many(2) });
    server.use(http.get("*/api/v1/push/runs/:id/entries.csv", () => new HttpResponse("x", { headers: { "Content-Disposition": 'attachment; filename="a.csv"', "X-Truncated": "true" } })));
    await user.click(await screen.findByRole("button", { name: /^Export CSV/ }));
    expect(await screen.findByText("The export was cut at 200,000 rows: narrow the filters")).toBeInTheDocument();
  });

  it("an error shows a message with a retry that downloads; nothing was saved before", async () => {
    const user = userEvent.setup();
    const dl = stubDownload();
    setup(`/entries?run=${RUN_ID}`, { entries: many(2) });
    let fail = true;
    server.use(http.get("*/api/v1/push/runs/:id/entries.csv", () => (fail ? HttpResponse.json({ detail: "boom" }, { status: 500 }) : new HttpResponse("x", { headers: { "Content-Disposition": 'attachment; filename="b.csv"' } }))));
    await user.click(await screen.findByRole("button", { name: /^Export CSV/ }));
    expect(await screen.findByText("The export failed")).toBeInTheDocument();
    expect(dl.downloads).toEqual([]);
    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(dl.downloads).toEqual(["b.csv"]));
    expect(screen.queryByText("The export failed")).not.toBeInTheDocument();
  });

  it("leaves the count out of the label for a very large result", async () => {
    setup(`/entries?run=${RUN_ID}`, { entries: [entry()] });
    server.use(http.get("*/api/v1/push/runs/:id/entries", () => HttpResponse.json({ items: [entry()], total: 25_000, counts: { failed: 25_000, skipped: 0, excluded: 0 }, entities: [] })));
    await table();
    expect(await screen.findByRole("button", { name: "Export CSV" })).toBeEnabled();
  });
});

describe("a11y smoke", () => {
  it("one h1, a labelled filter group, labelled controls, and a run link", async () => {
    setup(`/entries?run=${RUN_ID}`, { entries: [entry()] });
    await table();
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("group", { name: "Outcome" })).toBeInTheDocument();
    for (const label of ["Run", "Entity", "Reason", "Search"]) expect(screen.getByLabelText(label)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open the run" })).toHaveAttribute("href", `/runs/push/${RUN_ID}`);
  });
});
