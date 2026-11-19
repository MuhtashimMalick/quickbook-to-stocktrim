import { Hourglass, Play, Power } from "lucide-react";
import { useRef, useState } from "react";
import { useNavigate } from "react-router";
import { ApiError } from "../../api/client";
import type { Overview } from "../../api/types";
import { Button } from "../../components/Button";
import { Dialog } from "../../components/Dialog";
import { ErrorState } from "../../components/ErrorState";
import { Loading } from "../../components/Loading";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { TextLink } from "../../components/TextLink";
import { ICON_SIZE } from "../../lib/icons";
import { useOverview } from "../overview/api";
import { agoText } from "../overview/status";
import { useSettings } from "../settings/useSettings";
import { useRunNow } from "./api";

/** What the run will do with StockTrim, in plain words. Production runs only plan until the server allows them. */
export function writesSentence(writesEnabled: boolean, environment: string, allowProduction: boolean, host?: string | null): string {
  if (!writesEnabled) return "Writes are switched off on the server: this run will plan the changes but send nothing.";
  if (environment === "production" && !allowProduction) return "Production runs only plan until the server allows them: this run will plan the changes but send nothing.";
  return `Writes are ENABLED: if every check passes, this run sends changes to the StockTrim ${environment} account${host ? ` (${host})` : ""}.`;
}

const contactSentence = (age: number | null) => (age === null ? "It has never checked in." : `It last checked in ${agoText(age)}.`);

function runningLink(o: Overview): { to: string; what: string } | null {
  if (o.running_cycle) return { to: `/runs/${o.running_cycle.id}`, what: "A sync" };
  if (o.running_push) return { to: `/runs/push/${o.running_push.id}`, what: "A push run" };
  return null;
}

export function RunNowPanel() {
  const overview = useOverview();
  const settings = useSettings();
  const run = useRunNow();
  const navigate = useNavigate();
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const [refused, setRefused] = useState(false);

  const o = overview.data;
  const s = settings.data;
  const running = o ? runningLink(o) : null;
  const switchedOff = refused || o?.scheduler.enabled === false;

  const start = () =>
    run.mutate(undefined, {
      onSuccess: (out) => void navigate(`/runs/${out.cycle_id}`),
      onError: (error) => {
        if (!(error instanceof ApiError)) return; // network error: the dialog shows it with a retry
        if (error.status === 409) void overview.refetch();
        if (error.status === 403) setRefused(true);
        if (error.status === 409 || error.status === 403) setOpen(false);
      },
    });

  const failed = run.isError && !(run.error instanceof ApiError && [403, 409].includes(run.error.status));
  const blocked = Boolean(running) || switchedOff || !o || !s;

  return (
    <Panel title="Run a sync now">
      {overview.isPending && <Loading label="Loading the sync state" />}
      {overview.isError && !o && <ErrorState message="The current state could not be loaded." onRetry={() => void overview.refetch()} />}
      {o && (
        <div className="space-y-4">
          <p>Starts the same cycle as the automatic one: it reads QuickBooks, then syncs to StockTrim.</p>
          {switchedOff && (
            <Notice tone="neutral" icon={Power} title="Not available: runs are switched off on the server.">
              <p>The scheduler is disabled on the server (SCHEDULER_ENABLED), which also stops manual runs. Ask whoever runs the server to enable it.</p>
            </Notice>
          )}
          {!switchedOff && running && (
            <Notice tone="info" icon={Hourglass} title={`${running.what} is already running.`}>
              <TextLink to={running.to}>Follow it step by step</TextLink>
            </Notice>
          )}
          <Button
            ref={trigger}
            onClick={() => {
              run.reset();
              setOpen(true);
            }}
            disabled={blocked}
          >
            <Play size={ICON_SIZE} aria-hidden="true" />
            Run sync now
          </Button>
        </div>
      )}
      <Dialog open={open} onOpenChange={(next) => !run.isPending && setOpen(next)} returnFocus={() => trigger.current} title="Run a sync now?" description="Check what will happen, then confirm.">
        {o && s && (
          <div className="space-y-3">
            <p>
              The run waits for the QuickBooks Web Connector to check in. {contactSentence(o.last_qbwc_contact_age_seconds)} That can take minutes to hours, and we cannot hurry it.
            </p>
            <p className="font-medium">{writesSentence(s.stocktrim_writes_enabled, s.environment, s.scheduler_allow_production, s.stocktrim_host)}</p>
            <p>It follows every automatic safety check; if one fails, nothing is sent.</p>
          </div>
        )}
        {failed && <ErrorState title="The sync did not start" message="The server could not be reached. Nothing was started." />}
        <div role="status" aria-live="polite" className="sr-only">
          {run.isPending ? "Starting…" : ""}
        </div>
        <div className="flex flex-wrap justify-end gap-3">
          <Button variant="secondary" onClick={() => setOpen(false)} disabled={run.isPending}>
            Cancel
          </Button>
          <Button onClick={start} disabled={run.isPending}>
            {run.isPending ? "Starting…" : failed ? "Try again" : "Start the sync"}
          </Button>
        </div>
      </Dialog>
    </Panel>
  );
}
