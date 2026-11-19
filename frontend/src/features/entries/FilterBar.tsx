import { Check, Download } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { ReasonOut } from "../../api/types";
import { Button } from "../../components/Button";
import { Field } from "../../components/Field";
import { Select } from "../../components/Select";
import { StatusStamp } from "../../components/StatusStamp";
import { ICON_SIZE } from "../../lib/icons";
import { OUTCOMES, type EntryParams, type Outcome } from "./params";
import type { useCsvExport } from "./useCsvExport";

const SEARCH_DEBOUNCE_MS = 300;
const EXPORT_COUNT_LIMIT = 10_000;
const n = (v: number) => v.toLocaleString("en-GB");

/** A text box that writes to the URL 300 ms after the last keystroke (not on every one). */
function SearchBox({ value, onCommit }: { value: string; onCommit: (q: string) => void }) {
  const [text, setText] = useState(value);
  const [seen, setSeen] = useState(value);
  if (value !== seen) {
    // the URL changed elsewhere (Clear filters, browser Back): show it
    setSeen(value);
    setText(value);
  }
  useEffect(() => {
    if (text.trim() === value) return;
    const id = setTimeout(() => onCommit(text.trim()), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [text, value, onCommit]);
  return (
    <div className="min-w-64 flex-1">
      <Field label="Search" type="search" value={text} maxLength={100} placeholder="QuickBooks name, ListID or TxnID" aria-describedby="entries-search-help" onChange={(e) => setText(e.target.value)} />
      <p id="entries-search-help" className="mt-1 text-table text-ink-muted">
        Matches part of a name, key or ID. Searching by name can take a few seconds on very large runs.
      </p>
    </div>
  );
}

function OutcomeChip({ outcome, count, pressed, onToggle }: { outcome: Outcome; count: number | undefined; pressed: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      aria-pressed={pressed}
      onClick={onToggle}
      className={`inline-flex h-row cursor-pointer items-center gap-2 rounded-md border px-2 ${pressed ? "border-ink bg-paper" : "border-rule bg-sheet hover:border-ink"}`}
    >
      {pressed && <Check size={ICON_SIZE} aria-hidden="true" />}
      <StatusStamp status={outcome} />{" "}
      <span className="font-mono tabular-nums">{count === undefined ? "-" : n(count)}</span>
    </button>
  );
}

interface Props {
  params: EntryParams;
  counts: Record<string, number> | undefined;
  total: number | undefined;
  entities: string[];
  reasons: Record<string, ReasonOut> | undefined;
  csv: ReturnType<typeof useCsvExport>;
  onChange: (patch: Partial<EntryParams>) => void;
}

/** Sticky filter bar: outcome chips with live counts, entity, reason, search, and the CSV export. */
export function FilterBar({ params, counts, total, entities, reasons, csv, onChange }: Props) {
  const toggle = (o: Outcome) => {
    const has = params.outcomes.includes(o);
    if (has && params.outcomes.length === 1) return; // one outcome always stays selected
    onChange({ outcomes: OUTCOMES.filter((x) => (x === o ? !has : params.outcomes.includes(x))) });
  };
  const commitSearch = useCallback((q: string) => onChange({ q }), [onChange]);
  const reasonOptions = Object.entries(reasons ?? {}).sort(([, a], [, b]) => a.title.localeCompare(b.title));
  const busy = csv.status === "busy";
  const rows = total !== undefined && total > 0 && total <= EXPORT_COUNT_LIMIT ? ` (${n(total)} ${total === 1 ? "row" : "rows"})` : "";

  return (
    <div className="sticky top-0 z-10 space-y-3 border-b border-rule bg-paper py-3">
      <div role="group" aria-label="Outcome" className="flex flex-wrap gap-2">
        {OUTCOMES.map((o) => (
          <OutcomeChip key={o} outcome={o} count={counts?.[o]} pressed={params.outcomes.includes(o)} onToggle={() => toggle(o)} />
        ))}
      </div>
      <div className="flex flex-wrap items-start gap-3">
        <Select className="w-48" label="Entity" value={params.entity} onChange={(e) => onChange({ entity: e.target.value })}>
          <option value="">All entities</option>
          {[...new Set([...entities, ...(params.entity ? [params.entity] : [])])].sort().map((e) => (
            <option key={e} value={e}>
              {e}
            </option>
          ))}
        </Select>
        <Select className="w-64" label="Reason" value={params.code} onChange={(e) => onChange({ code: e.target.value })}>
          <option value="">All reasons</option>
          {params.code && !reasons?.[params.code] && <option value={params.code}>{params.code}</option>}
          {reasonOptions.map(([code, r]) => (
            <option key={code} value={code}>
              {r.title}
            </option>
          ))}
        </Select>
        <SearchBox value={params.q} onCommit={commitSearch} />
        <div className="pt-7">
          <Button variant="secondary" disabled={busy || total === 0} onClick={() => void csv.run()}>
            <Download size={ICON_SIZE} aria-hidden="true" />
            {busy ? "Preparing export..." : `Export CSV${rows}`}
          </Button>
          <span role="status" aria-live="polite" className="sr-only">
            {busy ? "Preparing export..." : ""}
          </span>
        </div>
      </div>
    </div>
  );
}
