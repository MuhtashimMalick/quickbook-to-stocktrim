import { CirclePause, FlaskConical, ListChecks, OctagonX, TriangleAlert } from "lucide-react";
import { useParams } from "react-router";
import { CopyCommand } from "../../components/CopyCommand";
import { ErrorState } from "../../components/ErrorState";
import { FreshnessNote } from "../../components/FreshnessNote";
import { LiveRegion, useRunAnnouncement } from "../../components/LiveRegion";
import { Loading } from "../../components/Loading";
import { Mono } from "../../components/Mono";
import { Notice } from "../../components/Notice";
import { Page } from "../../components/Page";
import { Panel } from "../../components/Panel";
import { StatusStamp } from "../../components/StatusStamp";
import { TextLink } from "../../components/TextLink";
import { Time } from "../../components/Time";
import { ICON_SIZE } from "../../lib/icons";
import { useConsecutiveFailures, useNow } from "../../lib/live";
import { RESUMABLE, stampFor } from "../../lib/status";
import { durationBetween } from "../../lib/time";
import { useEvents, usePushRun, useReasonTitle } from "./api";
import { cancelCommand, resumeCommand } from "./cli";
import { EventLog } from "./EventLog";
import { EventsStatus } from "./EventsStatus";
import { phaseTitle, runLedger } from "./ledger";
import { PhaseLedger } from "./PhaseLedger";
import { RunNotFound, isNotFound } from "./RunNotFound";
import { RunProgress } from "./RunProgress";

/** A push run on its own (for example a manual run from the server). Reuses the ledger, progress and event components of the cycle page. */
export default function PushRunDetailPage() {
  const { runId = "" } = useParams();
  const q = usePushRun(runId);
  const run = q.data;
  const running = run?.status === "running";
  const ev = useEvents({ run_id: run ? runId : undefined }, running);
  const failures = useConsecutiveFailures(q);
  const reasonTitle = useReasonTitle();
  const now = useNow();
  const events = ev.data?.items ?? [];
  const phase = running ? phaseTitle(events.at(-1)?.code) : null;
  const announcement = useRunAnnouncement(phase, "The run has finished");

  if (q.isError && isNotFound(q.error)) return <RunNotFound what="Push run" />;
  const resumable = run && !run.dry_run && RESUMABLE.includes(run.status);
  return (
    <Page title="Push run" purpose="One push to StockTrim: its steps, how far each got, and how it ended.">
      <TextLink to="/runs?view=push">All push runs</TextLink>
      {q.isPending && <Loading label="Loading the push run" />}
      {q.isError && !run && <ErrorState message="The push run could not be loaded." onRetry={() => void q.refetch()} />}
      {run && (
        <>
          <Panel>
            <div className="flex flex-wrap items-center justify-between gap-4">
              <dl className="grid grid-cols-[max-content_1fr] gap-x-8 gap-y-2">
                <dt className="text-ink-muted">Status</dt>
                <dd className="flex flex-wrap gap-1">
                  {run.dry_run && <StatusStamp status="dry-run" />}
                  <StatusStamp status={stampFor(run.status)} />
                </dd>
                <dt className="text-ink-muted">Run id</dt>
                <dd>
                  <Mono>{run.run_id}</Mono>
                </dd>
                <dt className="text-ink-muted">Steps</dt>
                <dd>{run.steps.length ? run.steps.join(", ") : "none"}</dd>
                <dt className="text-ink-muted">Started</dt>
                <dd>
                  <Time at={run.started_at} />
                </dd>
                <dt className="text-ink-muted">Finished</dt>
                <dd>{run.finished_at ? <Time at={run.finished_at} /> : <span className="text-ink-muted">still running</span>}</dd>
                <dt className="text-ink-muted">Duration</dt>
                <dd>
                  <Mono>{durationBetween(run.started_at, run.finished_at, now)}</Mono>
                </dd>
              </dl>
              <TextLink to={`/entries?run=${run.run_id}`} className="inline-flex items-center gap-2">
                <ListChecks size={ICON_SIZE} aria-hidden="true" />
                View entries
              </TextLink>
            </div>
            {running && <div className="mt-4"><FreshnessNote updatedAt={q.dataUpdatedAt} failures={failures} onRetry={() => void q.refetch()} /></div>}
          </Panel>

          {run.dry_run && (
            <Notice tone="neutral" icon={FlaskConical} title="Nothing was written to StockTrim">
              <p>This was a dry run: it planned the changes and ran the checks, and sent nothing.</p>
            </Notice>
          )}
          {run.status === "interrupted" && (
            <Notice tone="warn" icon={CirclePause} title="This run was interrupted">
              <p>The application stopped or restarted while it ran. Records already sent stay sent; resuming sends only the rest. It is a server action, so run this there:</p>
              <CopyCommand command={resumeCommand(run.run_id)} label="Command to resume the run" />
            </Notice>
          )}
          {resumable && run.status !== "interrupted" && (
            <Notice tone="warn" icon={TriangleAlert} title={`${reasonTitle(run.status)}: it can be resumed`}>
              <p>Resuming sends only the records that are not done yet. It is a server action, so run this there:</p>
              <CopyCommand command={resumeCommand(run.run_id)} label="Command to resume the run" />
            </Notice>
          )}
          {running && (
            <Notice tone="info" icon={OctagonX} title="To stop this run">
              <p>Stopping is a server action. Unsent records stay pending and the run can be resumed later:</p>
              <CopyCommand command={cancelCommand(run.run_id)} label="Command to cancel the run" />
            </Notice>
          )}

          <div className="grid gap-6 lg:grid-cols-5">
            <div className="lg:col-span-3">
              <Panel title="Steps">
                <PhaseLedger label="Steps of this run" steps={runLedger(events, run.status)} />
              </Panel>
            </div>
            <div className="lg:col-span-2">
              <Panel title="Progress">
                <RunProgress run={run} />
              </Panel>
            </div>
          </div>

          <Panel title="Event log">
            <EventsStatus queries={[ev]} />
            <EventLog events={events} />
          </Panel>
        </>
      )}
      <LiveRegion message={announcement} />
    </Page>
  );
}
