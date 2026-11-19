import { Link, useSearchParams } from "react-router";
import { EmptyState } from "../../components/EmptyState";
import { ErrorState } from "../../components/ErrorState";
import { Button } from "../../components/Button";
import { FreshnessNote } from "../../components/FreshnessNote";
import { Loading } from "../../components/Loading";
import { Page } from "../../components/Page";
import { Panel } from "../../components/Panel";
import { useConsecutiveFailures } from "../../lib/live";
import { useCycleList, useRunList } from "./api";
import { CycleTable } from "./CycleTable";
import { PushRunTable } from "./PushRunTable";

const TABS = [
  { view: "cycles", label: "Sync cycles", to: "/runs" },
  { view: "push", label: "Push runs", to: "/runs?view=push" },
] as const;

function CyclesSection() {
  const q = useCycleList();
  const failures = useConsecutiveFailures(q);
  const cycles = q.data?.pages.flat() ?? [];
  return (
    <Panel title="Sync cycles">
      {q.isPending && <Loading label="Loading sync cycles" />}
      {q.isError && !q.data && <ErrorState message="The sync cycles could not be loaded." onRetry={() => void q.refetch()} />}
      {q.data && (
        <div className="space-y-4">
          <FreshnessNote updatedAt={q.dataUpdatedAt} failures={failures} onRetry={() => void q.refetch()} />
          {cycles.length === 0 ? <EmptyState title="No sync cycle has run yet." hint="A cycle appears here as soon as the schedule fires or someone starts one." /> : <CycleTable cycles={cycles} />}
          {q.isFetchNextPageError && <ErrorState message="More cycles could not be loaded." onRetry={() => void q.fetchNextPage()} />}
          {q.hasNextPage && (
            <Button variant="secondary" disabled={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
              {q.isFetchingNextPage ? "Loading..." : "Load more"}
            </Button>
          )}
        </div>
      )}
    </Panel>
  );
}

function PushRunsSection() {
  const q = useRunList();
  const failures = useConsecutiveFailures(q);
  const runs = q.data?.pages.flat() ?? [];
  return (
    <Panel title="Push runs">
      <p className="mb-4 text-ink-muted">Every push to StockTrim, including the dry runs and the manual runs started from the server (those are not part of a cycle).</p>
      {q.isPending && <Loading label="Loading push runs" />}
      {q.isError && !q.data && <ErrorState message="The push runs could not be loaded." onRetry={() => void q.refetch()} />}
      {q.data && (
        <div className="space-y-4">
          <FreshnessNote updatedAt={q.dataUpdatedAt} failures={failures} onRetry={() => void q.refetch()} />
          {runs.length === 0 ? <EmptyState title="No push run yet." hint="Runs appear here once something has been planned or sent to StockTrim." /> : <PushRunTable runs={runs} />}
          {q.isFetchNextPageError && <ErrorState message="More runs could not be loaded." onRetry={() => void q.fetchNextPage()} />}
          {q.hasNextPage && (
            <Button variant="secondary" disabled={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()}>
              {q.isFetchingNextPage ? "Loading..." : "Load more"}
            </Button>
          )}
        </div>
      )}
    </Panel>
  );
}

export default function RunsPage() {
  const [params] = useSearchParams();
  const view = params.get("view") === "push" ? "push" : "cycles";
  return (
    <Page title="Runs" purpose="Every sync cycle and push run, newest first, with how each one ended.">
      <nav aria-label="Run lists" className="flex gap-5 border-b border-rule">
        {TABS.map((t) => (
          <Link
            key={t.view}
            to={t.to}
            aria-current={view === t.view ? "page" : undefined}
            className={`inline-flex h-row items-center border-b-2 px-1 font-medium ${view === t.view ? "border-accent text-accent" : "border-transparent text-ink-muted hover:text-ink"}`}
          >
            {t.label}
          </Link>
        ))}
      </nav>
      {view === "push" ? <PushRunsSection /> : <CyclesSection />}
    </Page>
  );
}
