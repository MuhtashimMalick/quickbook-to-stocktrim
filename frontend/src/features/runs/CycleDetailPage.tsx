import { useParams } from "react-router";
import { ListChecks } from "lucide-react";
import { ErrorState } from "../../components/ErrorState";
import { FreshnessNote } from "../../components/FreshnessNote";
import { LiveRegion, useRunAnnouncement } from "../../components/LiveRegion";
import { Loading } from "../../components/Loading";
import { Page } from "../../components/Page";
import { Panel } from "../../components/Panel";
import { StatusStamp } from "../../components/StatusStamp";
import { TextLink } from "../../components/TextLink";
import { Time } from "../../components/Time";
import { Mono } from "../../components/Mono";
import { ICON_SIZE } from "../../lib/icons";
import { useConsecutiveFailures, useNow } from "../../lib/live";
import { stampFor } from "../../lib/status";
import { isUuid } from "../../lib/uuid";
import { durationBetween } from "../../lib/time";
import { useCycle, useEvents, useReasonTitle } from "./api";
import { cycleLedger, phaseTitle } from "./ledger";
import { EventLog } from "./EventLog";
import { OutcomePanel } from "./OutcomePanel";
import { PhaseLedger } from "./PhaseLedger";
import { RunProgress } from "./RunProgress";
import { RunNotFound, isNotFound } from "./RunNotFound";
import { EventsStatus } from "./EventsStatus";

export default function CycleDetailPage() {
  const { cycleId = "" } = useParams();
  const q = useCycle(cycleId);
  const cycle = q.data;
  const running = cycle?.status === "running";
  const cycleEvents = useEvents({ cycle_id: cycle ? cycleId : undefined }, running);
  const runEvents = useEvents({ run_id: cycle?.push_run_id ?? undefined }, running);
  const failures = useConsecutiveFailures(q);
  const reasonTitle = useReasonTitle();
  const now = useNow();

  const events = [...(cycleEvents.data?.items ?? []), ...(runEvents.data?.items ?? [])].sort((a, b) => a.id - b.id);
  const newest = events.at(-1);
  const phase = running ? phaseTitle(newest?.code) : null;
  const announcement = useRunAnnouncement(phase, "The cycle has finished");

  if (!isUuid(cycleId) || (q.isError && isNotFound(q.error))) return <RunNotFound what="Sync cycle" />;
  return (
    <Page title="Sync cycle" purpose="What this cycle did, step by step, and how it ended.">
      <TextLink to="/runs">All runs</TextLink>
      {q.isPending && <Loading label="Loading the cycle" />}
      {q.isError && !cycle && <ErrorState message="The cycle could not be loaded." onRetry={() => void q.refetch()} />}
      {cycle && (
        <>
          <Panel>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <dl className="grid grid-cols-[max-content_1fr] gap-x-8 gap-y-2">
                <dt className="text-ink-muted">Status</dt>
                <dd>
                  <StatusStamp status={stampFor(cycle.status)} />
                </dd>
                <dt className="text-ink-muted">Trigger</dt>
                <dd>{cycle.trigger}</dd>
                <dt className="text-ink-muted">Started</dt>
                <dd>
                  <Time at={cycle.started_at} />
                </dd>
                <dt className="text-ink-muted">Finished</dt>
                <dd>{cycle.finished_at ? <Time at={cycle.finished_at} /> : <span className="text-ink-muted">still running</span>}</dd>
                <dt className="text-ink-muted">Duration</dt>
                <dd>
                  <Mono>{durationBetween(cycle.started_at, cycle.finished_at, now)}</Mono>
                </dd>
                {cycle.push_run_id && (
                  <>
                    <dt className="text-ink-muted">Push run</dt>
                    <dd>
                      <TextLink to={`/runs/push/${cycle.push_run_id}`}>
                        <Mono>{cycle.push_run_id.slice(0, 8)}</Mono>
                      </TextLink>
                    </dd>
                  </>
                )}
              </dl>
              {cycle.push_run_id && (
                <TextLink to={`/entries?run=${cycle.push_run_id}`} className="inline-flex items-center gap-2">
                  <ListChecks size={ICON_SIZE} aria-hidden="true" />
                  View entries
                </TextLink>
              )}
            </div>
            {running && <div className="mt-4"><FreshnessNote updatedAt={q.dataUpdatedAt} failures={failures} onRetry={() => void q.refetch()} /></div>}
          </Panel>

          <div className="grid gap-6 lg:grid-cols-5">
            <div className="lg:col-span-3">
              <Panel title="Phases">
                <PhaseLedger label="Phases of this cycle" steps={cycleLedger(events, cycle.status, cycle.reason ? reasonTitle(cycle.reason) : undefined)} />
              </Panel>
            </div>
            <div className="space-y-6 lg:col-span-2">
              <OutcomePanel cycle={cycle} />
              {cycle.push_run && (
                <Panel title="Progress">
                  <RunProgress run={cycle.push_run} />
                </Panel>
              )}
            </div>
          </div>

          <Panel title="Event log">
            <EventsStatus queries={[cycleEvents, runEvents]} />
            <EventLog events={events} />
          </Panel>
        </>
      )}
      <LiveRegion message={announcement} />
    </Page>
  );
}
