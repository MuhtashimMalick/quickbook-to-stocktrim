import { ListChecks, Scissors } from "lucide-react";
import { useCallback, useEffect, useRef } from "react";
import type { PushRunOut, RunListItem } from "../../api/types";
import { Button } from "../../components/Button";
import { EmptyState } from "../../components/EmptyState";
import { ErrorState } from "../../components/ErrorState";
import { Loading } from "../../components/Loading";
import { Mono } from "../../components/Mono";
import { Notice } from "../../components/Notice";
import { Page } from "../../components/Page";
import { Panel } from "../../components/Panel";
import { Select } from "../../components/Select";
import { StatusStamp } from "../../components/StatusStamp";
import { TextLink } from "../../components/TextLink";
import { ICON_SIZE } from "../../lib/icons";
import { stampFor } from "../../lib/status";
import { useFormatters } from "../../lib/useFormatters";
import { useReasonCodes } from "../runs/api";
import { isNotFound, RunNotFound } from "../runs/RunNotFound";
import { defaultRun, useEntries, useEntry, useRecentRuns, useRunMeta } from "./api";
import { EntriesTable, TableSkeleton } from "./EntriesTable";
import { EntryDrawer } from "./EntryDrawer";
import { FilterBar } from "./FilterBar";
import { DEFAULT_OUTCOMES, entryId, hasNarrowing, PAGE_SIZE, useEntryParams } from "./params";
import { CSV_CAP, useCsvExport } from "./useCsvExport";

const TITLE = "Entries";
const PURPOSE = "The records of one push run that failed, were skipped or were left out, with their QuickBooks identifiers.";
const n = (v: number) => v.toLocaleString("en-GB");

type RunChoice = Pick<RunListItem, "id" | "started_at" | "dry_run" | "status" | "mode">;

function RunPicker({ runId, runs, current, onPick }: { runId: string; runs: RunListItem[]; current: PushRunOut | undefined; onPick: (id: string) => void }) {
  const f = useFormatters();
  const choices: RunChoice[] = runs.some((r) => r.id === runId)
    ? runs
    : [...(current ? [{ id: runId, started_at: current.started_at, dry_run: current.dry_run, status: current.status, mode: current.mode }] : [{ id: runId, started_at: "", dry_run: false, status: "", mode: null }]), ...runs];
  const label = (r: RunChoice) => [r.started_at ? f.dateTime(r.started_at) : r.id.slice(0, 8), r.mode ?? "push", r.dry_run ? "dry run" : null, r.status.replaceAll("_", " ")].filter(Boolean).join(" - ");
  return (
    <div className="flex flex-wrap items-end gap-3">
      <Select className="w-full max-w-lg" label="Run" value={runId} onChange={(e) => onPick(e.target.value)}>
        {choices.map((r) => (
          <option key={r.id} value={r.id}>
            {label(r)}
          </option>
        ))}
      </Select>
      <div className="flex flex-wrap items-center gap-2 pb-1">
        {current?.dry_run && <StatusStamp status="dry-run" />}
        {current && <StatusStamp status={stampFor(current.status)} />}
        <TextLink to={`/runs/push/${runId}`} className="inline-flex items-center gap-1">
          <ListChecks size={ICON_SIZE} aria-hidden="true" />
          Open the run
        </TextLink>
      </div>
    </div>
  );
}

function Pager({ page, pages, onPage }: { page: number; pages: number; onPage: (p: number) => void }) {
  if (pages <= 1) return null;
  return (
    <nav aria-label="Pages" className="flex items-center justify-between gap-3 pt-4">
      <Button variant="secondary" disabled={page <= 1} onClick={() => onPage(page - 1)}>
        Previous page
      </Button>
      <span className="text-ink-muted">
        Page <Mono>{n(page)}</Mono> of <Mono>{n(pages)}</Mono>
      </span>
      <Button variant="secondary" disabled={page >= pages} onClick={() => onPage(page + 1)}>
        Next page
      </Button>
    </nav>
  );
}

function RunEntries({ runId, runs }: { runId: string; runs: RunListItem[] }) {
  const { params, update, setFilters } = useEntryParams();
  const meta = useRunMeta(runId);
  const entries = useEntries(runId, params, meta.data?.status === "running");
  const catalog = useReasonCodes().data;
  const csv = useCsvExport(runId, params);
  const data = entries.data;
  const items = data?.items ?? [];
  const { entry, loading } = useEntry(runId, params.entry, data?.items);
  const index = items.findIndex((e) => entryId(e) === params.entry);
  const lastOpened = useRef<string | null>(null);
  useEffect(() => {
    if (params.entry) lastOpened.current = params.entry;
  });
  const returnFocus = useCallback(
    () => Array.from(document.querySelectorAll<HTMLElement>("[data-entry-id]")).find((el) => el.dataset.entryId === lastOpened.current) ?? null,
    [],
  );

  const pages = data ? Math.max(1, Math.ceil(data.total / PAGE_SIZE)) : 1;
  useEffect(() => {
    // filters can shrink the result below the current page: step back to the last page that exists
    if (data && !entries.isPlaceholderData && data.total > 0 && items.length === 0 && params.page > pages) update({ page: pages });
  }, [data, entries.isPlaceholderData, items.length, params.page, pages, update]);

  if ((entries.isError && isNotFound(entries.error)) || (meta.isError && isNotFound(meta.error))) return <RunNotFound what="Run" />;

  const entities = [...Object.keys(meta.data?.counts ?? {}), ...items.map((e) => e.entity)];
  const counts = data?.counts;
  const total = data?.total;
  const from = (params.page - 1) * PAGE_SIZE + 1;
  const showExcluded = () => setFilters({ outcomes: [...DEFAULT_OUTCOMES, "excluded"] });

  let body;
  if (entries.isPending) {
    body = <TableSkeleton />;
  } else if (entries.isError && !data) {
    body = <ErrorState message="The entries could not be loaded." onRetry={() => void entries.refetch()} />;
  } else if (data && data.total === 0) {
    const none = Object.values(data.counts).every((c) => c === 0);
    if (!hasNarrowing(params) && none) {
      body = (
        <EmptyState
          title="Nothing needs attention."
          hint={meta.data?.dry_run ? "No entry in this dry run failed, was skipped or was left out." : "Every entry in this run was sent."}
        />
      );
    } else if (!hasNarrowing(params) && !params.outcomes.includes("excluded") && data.counts.excluded) {
      body = (
        <div className="space-y-3">
          <EmptyState title="No failed or skipped entries in this run." hint={`${n(data.counts.excluded)} records were left out (excluded) before sending.`} />
          <Button variant="secondary" onClick={showExcluded}>
            Show excluded entries
          </Button>
        </div>
      );
    } else {
      body = (
        <div className="space-y-3">
          <EmptyState title="No entries match these filters." hint="Try a shorter search, another reason, or another outcome." />
          <Button variant="secondary" onClick={() => setFilters({ outcomes: DEFAULT_OUTCOMES, entity: "", code: "", q: "" })}>
            Clear filters
          </Button>
        </div>
      );
    }
  } else {
    body = (
      <>
        <EntriesTable items={items} catalog={catalog} onOpen={(id) => update({ entry: id }, false)} />
        <Pager page={params.page} pages={pages} onPage={(page) => update({ page, entry: null })} />
      </>
    );
  }

  return (
    <Page title={TITLE} purpose={PURPOSE}>
      <RunPicker runId={runId} runs={runs} current={meta.data} onPick={(run) => setFilters({ run, entity: "", code: "", q: "" })} />
      <div>
        <FilterBar params={params} counts={counts} total={total} entities={entities} reasons={catalog} csv={csv} onChange={(patch) => setFilters(patch)} />
        {csv.truncated && (
          <div className="mt-3">
            <Notice tone="warn" icon={Scissors} title={`The export was cut at ${n(CSV_CAP)} rows: narrow the filters`}>
              <p>The file holds the {n(CSV_CAP)} most recently updated entries that match.</p>
            </Notice>
          </div>
        )}
        {csv.status === "error" && (
          <div className="mt-3">
            <ErrorState title="The export failed" message="The CSV file could not be prepared. Nothing was downloaded." onRetry={() => void csv.run()} />
          </div>
        )}
        <p role="status" aria-live="polite" className="py-3 text-ink-muted">
          {data && data.total > 0 ? (
            <>
              Showing <Mono>{n(from)}</Mono>-<Mono>{n(Math.min(from + PAGE_SIZE - 1, data.total))}</Mono> of <Mono>{n(data.total)}</Mono>
            </>
          ) : (
            ""
          )}
        </p>
        <Panel>
          <div aria-busy={entries.isFetching}>{body}</div>
        </Panel>
      </div>
      <EntryDrawer
        open={params.entry !== null}
        entry={entry}
        loading={loading}
        catalog={catalog}
        position={index >= 0 ? { index, count: items.length } : null}
        onStep={(delta) => {
          const next = items[index + delta];
          if (next) update({ entry: entryId(next) });
        }}
        onClose={() => update({ entry: null })}
        returnFocus={returnFocus}
      />
    </Page>
  );
}

/** Entries of one push run. The run is chosen in the URL (`?run=`); without one it defaults to the latest real (non-dry) run. */
export default function EntriesPage() {
  const { params, update } = useEntryParams();
  const runs = useRecentRuns();
  const fallback = defaultRun(runs.data)?.id;

  useEffect(() => {
    if (!params.run && fallback) update({ run: fallback }); // pin it, so the shared link means the same run
  }, [params.run, fallback, update]);

  const runId = params.run ?? fallback;
  if (!runId) {
    return (
      <Page title={TITLE} purpose={PURPOSE}>
        {runs.isPending && <Loading label="Loading the runs" />}
        {runs.isError && <ErrorState message="The runs could not be loaded." onRetry={() => void runs.refetch()} />}
        {runs.data && (
          <Panel>
            <EmptyState title="No sync has run yet" hint="Entries appear here once a push run has finished." />
            <p className="mt-3">
              <TextLink to="/runs">Go to Runs</TextLink>
            </p>
          </Panel>
        )}
      </Page>
    );
  }
  return <RunEntries runId={runId} runs={runs.data ?? []} />;
}
