import { CircleCheck, CircleHelp, CirclePause, FlaskConical, PencilLine, TriangleAlert } from "lucide-react";
import type { CycleDetail } from "../../api/types";
import { CopyCommand } from "../../components/CopyCommand";
import { Mono } from "../../components/Mono";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { TextLink } from "../../components/TextLink";
import { ICON_SIZE } from "../../lib/icons";
import { RESUMABLE } from "../../lib/status";
import { resumeCommand } from "./cli";

/** How many records a real run has already sent. */
export function sentCount(run: CycleDetail["push_run"]): number {
  if (!run || run.dry_run) return 0;
  return Object.values(run.counts).reduce((n, c) => n + (c.done ?? 0), 0);
}

/** The one line that answers "did this change anything in StockTrim?": yes, no, or not certain (calls that ended without a clear answer). */
export function WrittenLine({ cycle }: { cycle: CycleDetail }) {
  const sent = sentCount(cycle.push_run);
  if (cycle.wrote_to_stocktrim === "yes") {
    return (
      <p className="flex items-start gap-2 font-semibold">
        <PencilLine size={ICON_SIZE} aria-hidden="true" className="mt-1 shrink-0" />
        <span>
          Changes were sent to StockTrim (<Mono>{sent.toLocaleString("en-GB")}</Mono> {sent === 1 ? "record" : "records"}).
        </span>
      </p>
    );
  }
  if (cycle.wrote_to_stocktrim === "unknown") {
    return (
      <p className="flex items-start gap-2 font-semibold">
        <CircleHelp size={ICON_SIZE} aria-hidden="true" className="mt-1 shrink-0" />
        <span>
          It is not certain whether StockTrim applied some records: their calls ended without a clear answer. See{" "}
          {cycle.push_run_id ? <TextLink to={`/entries?run=${cycle.push_run_id}&outcome=failed`}>Entries</TextLink> : "Entries"} (reason: unknown outcome).
        </span>
      </p>
    );
  }
  return (
    <p className="flex items-start gap-2 font-semibold">
      <FlaskConical size={ICON_SIZE} aria-hidden="true" className="mt-1 shrink-0" />
      <span>Nothing was written to StockTrim.</span>
    </p>
  );
}

/** How a finished cycle ended: reason code, the catalog's explanation and next step, and whether anything was written. */
export function OutcomePanel({ cycle }: { cycle: CycleDetail }) {
  if (cycle.status === "running") return null;
  const info = cycle.reason_info;
  const run = cycle.push_run;
  const resumable = cycle.push_run_id && run && !run.dry_run && RESUMABLE.includes(run.status);
  return (
    <Panel title="Outcome">
      <div className="space-y-4">
        <WrittenLine cycle={cycle} />
        {cycle.status === "done" && !cycle.reason && (
          <p className="flex items-start gap-2 text-ink-muted">
            <CircleCheck size={ICON_SIZE} aria-hidden="true" className="mt-1 shrink-0" />
            The cycle finished and every step completed.
          </p>
        )}
        {cycle.reason && (
          <div className="space-y-1">
            <p>
              <Mono className="text-table text-ink-muted">{cycle.reason}</Mono>
            </p>
            <p className="font-semibold">{info?.title ?? cycle.reason}</p>
            {info && (
              <>
                <p>{info.explanation}</p>
                <p>
                  <span className="font-semibold">Next step: </span>
                  {info.next_step}
                </p>
              </>
            )}
          </div>
        )}
        {cycle.status === "interrupted" && (
          <Notice tone="warn" icon={CirclePause} title="This cycle was interrupted">
            <p>A cycle cannot be resumed; the next scheduled or manual run starts a fresh one.</p>
            {resumable && (
              <>
                <p>Its push run had started sending and can be resumed from the server. Run this there:</p>
                <CopyCommand command={resumeCommand(cycle.push_run_id as string)} label="Command to resume the push run" />
              </>
            )}
          </Notice>
        )}
        {cycle.status !== "interrupted" && resumable && (
          <Notice tone="warn" icon={TriangleAlert} title="The push run can be resumed">
            <p>Resuming sends only the records that are not done yet. It is a server action, so run this there:</p>
            <CopyCommand command={resumeCommand(cycle.push_run_id as string)} label="Command to resume the push run" />
          </Notice>
        )}
      </div>
    </Panel>
  );
}
