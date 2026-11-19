import { Activity, CircleSlash, Hourglass, KeyRound, PencilLine } from "lucide-react";
import type { Overview, SettingsOut } from "../../api/types";
import { EmptyState } from "../../components/EmptyState";
import { ErrorState } from "../../components/ErrorState";
import { Mono } from "../../components/Mono";
import { Panel } from "../../components/Panel";
import { StatusStamp } from "../../components/StatusStamp";
import { DataTable, RowTh, Td, Th } from "../../components/Table";
import { TextLink } from "../../components/TextLink";
import { Relative, Time } from "../../components/Time";
import { ICON_SIZE } from "../../lib/icons";
import { useNow } from "../../lib/live";
import { stampFor } from "../../lib/status";
import { usePushRun } from "../runs/api";
import { Counts } from "../runs/Counts";
import { EntityProgress } from "../runs/RunProgress";
import { agoText, classifyContact, isSnapshotStale, runningPhaseTitle } from "./status";

export function CurrentActivity({ overview }: { overview: Overview }) {
  const running = overview.running_cycle ?? overview.running_push;
  const run = usePushRun(overview.running_push?.id);
  if (!running) {
    return (
      <Panel title="Current activity">
        <p className="flex items-center gap-2">
          <CircleSlash size={ICON_SIZE} aria-hidden="true" className="text-ink-muted" />
          Idle. Nothing is running.
        </p>
      </Panel>
    );
  }
  const waiting = running.phase === "waiting_for_web_connector";
  const Icon = waiting ? Hourglass : Activity;
  return (
    <Panel title="Current activity">
      <div className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <StatusStamp status="running" label={waiting ? "Waiting" : "Running"} />
          <span className="flex items-center gap-2 font-medium">
            <Icon size={ICON_SIZE} aria-hidden="true" />
            {runningPhaseTitle(running) || "Starting"}
          </span>
          <span className="text-ink-muted">
            since <Relative at={running.since} />
          </span>
        </div>
        {waiting && (
          <p className="text-ink-muted">
            The reads are queued. QuickBooks only talks to us when the Web Connector on the client&apos;s PC connects
            {overview.last_qbwc_contact_age_seconds !== null && <>; it last did {agoText(overview.last_qbwc_contact_age_seconds)}</>}.
          </p>
        )}
        {run.data && Object.keys(run.data.counts).length > 0 && (
          <ul className="space-y-3">
            {Object.entries(run.data.counts).map(([entity, counts]) => (
              <EntityProgress key={entity} entity={entity} counts={counts} />
            ))}
          </ul>
        )}
        <TextLink to={overview.running_cycle ? `/runs/${overview.running_cycle.id}` : `/runs/push/${running.id}`}>Follow it step by step</TextLink>
      </div>
    </Panel>
  );
}

const CONTACT = {
  recent: { status: "done", label: "Recent", text: "The Web Connector checked in recently." },
  late: { status: "aborted", label: "Late", text: "The Web Connector has not checked in for a while. It runs on the client's PC on its own schedule." },
  silent: { status: "failed", label: "Silent", text: "No contact for hours. Check that the client's PC is on, QuickBooks is open and Auto-Run is set." },
  never: { status: "skipped", label: "Never connected", text: "The Web Connector has not connected yet. Load the .qwc file in it on the client's PC." },
} as const;

export function WebConnectorPanel({ overview }: { overview: Overview }) {
  const age = overview.last_qbwc_contact_age_seconds;
  const kind = classifyContact(age);
  const c = CONTACT[kind];
  return (
    <Panel title="Web Connector">
      <div className="space-y-3">
        <StatusStamp status={c.status} label={c.label} />
        <p>{c.text}</p>
        {overview.last_qbwc_contact && age !== null && (
          <p className="text-ink-muted">
            Last contact <Time at={overview.last_qbwc_contact} /> ({agoText(age)})
          </p>
        )}
        {overview.last_qbwc_auth_refused && (
          <p className="flex items-start gap-2 text-aborted">
            <KeyRound size={ICON_SIZE} aria-hidden="true" className="mt-1 shrink-0" />
            <span>
              The Web Connector&apos;s login was refused <Time at={overview.last_qbwc_auth_refused} />. Its saved username or password does not match the server.
            </span>
          </p>
        )}
      </div>
    </Panel>
  );
}

export function SnapshotTable({ overview }: { overview: Overview }) {
  const now = useNow();
  const rows = Object.entries(overview.snapshot).sort(([a], [b]) => a.localeCompare(b));
  const every = overview.scheduler.every_days;
  return (
    <Panel title="QuickBooks snapshot">
      {rows.length === 0 ? (
        <EmptyState title="Nothing has been read from QuickBooks yet." hint="The first read happens when the Web Connector next connects after a sync is queued." />
      ) : (
        <>
          <DataTable caption="Freshness of each QuickBooks table">
            <thead>
              <tr>
                <Th>Table</Th>
                <Th align="right">Records</Th>
                <Th>Last synced</Th>
                <Th>Freshness</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map(([entity, f]) => {
                const stale = isSnapshotStale(f.synced_at_max, every, now);
                return (
                  <tr key={entity}>
                    <RowTh>{entity}</RowTh>
                    <Td align="right">
                      <Mono>{f.records.toLocaleString("en-GB")}</Mono>
                    </Td>
                    <Td>
                      {f.synced_at_max ? (
                        <>
                          <Time at={f.synced_at_max} /> <span className="text-ink-muted">(<Relative at={f.synced_at_max} />)</span>
                        </>
                      ) : (
                        <span className="text-ink-muted">never</span>
                      )}
                    </Td>
                    <Td>{stale ? <StatusStamp status="aborted" label="Stale" /> : <StatusStamp status="done" label="Current" />}</Td>
                  </tr>
                );
              })}
            </tbody>
          </DataTable>
          <p className="mt-3 text-table text-ink-muted">Stale means older than the sync interval (every {every === 1 ? "day" : `${every} days`}) plus a few hours of grace.</p>
        </>
      )}
    </Panel>
  );
}

export function LastPushPanel({ overview }: { overview: Overview }) {
  const run = overview.last_real_push_run;
  return (
    <Panel title="Latest real push">
      {!run ? (
        <EmptyState title="No real push has run yet." hint="Dry runs plan and check only; a real push is the first one that sends to StockTrim." />
      ) : (
        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-3">
            <StatusStamp status={stampFor(run.status)} />
            <Time at={run.started_at} />
          </div>
          {Object.values(run.counts ?? {}).some((n) => n > 0) ? (
            <div>
              <Counts counts={run.counts} />
            </div>
          ) : (
            <p className="text-ink-muted">No records needed sending in this run.</p>
          )}
          <div>
            <TextLink to={`/runs/push/${run.run_id}`}>Open the run</TextLink>
          </div>
        </div>
      )}
    </Panel>
  );
}

export function FactsPanel({ overview, settings, onRetry }: { overview: Overview; settings: SettingsOut | undefined; onRetry: () => void }) {
  const s = overview.scheduler;
  return (
    <Panel title="Server facts">
      {!settings ? (
        <ErrorState message="The server settings could not be loaded." onRetry={onRetry} />
      ) : (
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2">
          <dt className="text-ink-muted">Environment</dt>
          <dd>
            <Mono>{settings.environment}</Mono>
          </dd>
          <dt className="text-ink-muted">Writes</dt>
          <dd className="flex items-center gap-2">
            <PencilLine size={ICON_SIZE} aria-hidden="true" />
            {settings.stocktrim_writes_enabled ? "Enabled" : "Disabled"}
          </dd>
          <dt className="text-ink-muted">Scheduler</dt>
          <dd>{s.enabled ? "Allowed by the server" : "Off on the server (the ceiling)"}</dd>
          <dt className="text-ink-muted">Schedule</dt>
          <dd>
            Every {s.every_days === 1 ? "day" : `${s.every_days} days`} at <Mono>{s.time}</Mono> <Mono>{s.tz}</Mono>, {s.schedule_enabled ? "switched on" : "paused"}
            <span className="block text-table text-ink-muted">{s.source === "database" ? "Set in this app" : "Server default, not changed yet"}</span>
          </dd>
        </dl>
      )}
      <p className="mt-4">
        <TextLink to="/operations">Operations: schedule and run now</TextLink>
      </p>
    </Panel>
  );
}
