import { CircleSlash, DatabaseZap } from "lucide-react";
import { ErrorState } from "../../components/ErrorState";
import { FreshnessNote } from "../../components/FreshnessNote";
import { LiveRegion, useRunAnnouncement } from "../../components/LiveRegion";
import { Loading } from "../../components/Loading";
import { Notice } from "../../components/Notice";
import { Page } from "../../components/Page";
import { Panel } from "../../components/Panel";
import { TextLink } from "../../components/TextLink";
import { EmptyState } from "../../components/EmptyState";
import { useConsecutiveFailures } from "../../lib/live";
import { useFormatters } from "../../lib/useFormatters";
import { useRecentCycles } from "../runs/api";
import { CycleTable } from "../runs/CycleTable";
import { phaseTitle } from "../runs/ledger";
import { useSettings } from "../settings/useSettings";
import { isLive, useOverview } from "./api";
import { CurrentActivity, FactsPanel, LastPushPanel, SnapshotTable, WebConnectorPanel } from "./panels";
import { statusSentence } from "./status";

export default function OverviewPage() {
  const q = useOverview();
  const settings = useSettings();
  const o = q.data;
  const cycles = useRecentCycles(5, isLive(o));
  const failures = useConsecutiveFailures(q);
  const f = useFormatters();
  const running = o?.running_cycle ?? o?.running_push;
  const announcement = useRunAnnouncement(running ? phaseTitle(running.phase) || "starting" : null, "The sync has finished");

  return (
    <Page title="Overview" purpose="What the sync is doing right now, and whether anything needs attention.">
      {q.isPending && <Loading label="Loading the overview" />}
      {q.isError && !o && <ErrorState message="The overview could not be loaded." onRetry={() => void q.refetch()} />}
      {o && (
        <>
          <section aria-label="Status" className="space-y-3">
            <p className="text-heading">{statusSentence(o, f)}</p>
            {settings.data && !settings.data.stocktrim_writes_enabled && (
              <Notice tone="neutral" icon={CircleSlash} title="Writes are off">
                <p>Writes are switched off on the server, so runs plan the changes but send nothing.</p>
              </Notice>
            )}
            {!o.database && (
              <Notice tone="warn" icon={DatabaseZap} title="The database is not reachable">
                <p>The server could not answer from its database. Counts and times below may be empty or old. It usually clears by itself; if it does not, check the db service.</p>
              </Notice>
            )}
            <FreshnessNote updatedAt={q.dataUpdatedAt} failures={failures} onRetry={() => void q.refetch()} />
          </section>

          <div className="grid gap-6 lg:grid-cols-3">
            <div className="space-y-6 lg:col-span-2">
              <CurrentActivity overview={o} />
              <SnapshotTable overview={o} />
              <Panel title="Last 5 cycles">
                {cycles.isPending && <Loading label="Loading recent cycles" />}
                {cycles.isError && <ErrorState message="Recent cycles could not be loaded." onRetry={() => void cycles.refetch()} />}
                {cycles.data &&
                  (cycles.data.length === 0 ? (
                    <EmptyState title="No sync cycle has run yet." hint="The first one appears here when the schedule fires or someone starts it." />
                  ) : (
                    <div className="space-y-3">
                      <CycleTable cycles={cycles.data} compact caption="The five most recent sync cycles" />
                      <TextLink to="/runs">All runs</TextLink>
                    </div>
                  ))}
              </Panel>
            </div>
            <div className="space-y-6">
              <WebConnectorPanel overview={o} />
              <LastPushPanel overview={o} />
              <FactsPanel overview={o} settings={settings.data} onRetry={() => void settings.refetch()} />
            </div>
          </div>
        </>
      )}
      <LiveRegion message={announcement} />
    </Page>
  );
}
