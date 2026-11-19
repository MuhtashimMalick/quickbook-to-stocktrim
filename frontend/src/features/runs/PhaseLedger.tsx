import { Circle, CircleCheck, CircleMinus, CircleX, LoaderCircle, type LucideIcon } from "lucide-react";
import { Time } from "../../components/Time";
import { ICON_SIZE } from "../../lib/icons";
import { Counts } from "./Counts";
import type { LedgerStep, StepState } from "./ledger";

// Every state is icon + word + colour.
const STATE: Record<StepState, { word: string; icon: LucideIcon; classes: string }> = {
  done: { word: "Done", icon: CircleCheck, classes: "text-done" },
  current: { word: "In progress", icon: LoaderCircle, classes: "text-running" },
  pending: { word: "Waiting", icon: Circle, classes: "text-ink-muted" },
  failed: { word: "Stopped here", icon: CircleX, classes: "text-failed" },
  skipped: { word: "Not run", icon: CircleMinus, classes: "text-skipped" },
};

/** A vertical ledger of the steps of a run, ticked as they happen. */
export function PhaseLedger({ steps, label }: { steps: LedgerStep[]; label: string }) {
  return (
    <ol aria-label={label} className="border-l border-rule">
      {steps.map((s) => {
        const { word, icon: Icon, classes } = STATE[s.state];
        return (
          <li key={s.id} className={`relative py-2 pr-2 ${s.nested ? "pl-12" : "pl-6"}`}>
            <span className={`absolute top-3 flex -translate-x-1/2 bg-sheet ${s.nested ? "left-6" : "left-0"} ${classes}`}>
              <Icon size={ICON_SIZE} aria-hidden="true" />
            </span>
            <div className="flex flex-wrap items-baseline gap-x-3">
              <span className={s.state === "pending" || s.state === "skipped" ? "text-ink-muted" : "font-medium"}>{s.title}</span>
              <span className={`text-table font-semibold uppercase tracking-wide ${classes}`}>{word}</span>
              {s.at && <Time at={s.at} format="clock" />}
            </div>
            {(s.note || s.counts) && (
              <div className="text-table text-ink-muted">
                {s.note}
                {s.counts && <Counts counts={s.counts} />}
              </div>
            )}
          </li>
        );
      })}
    </ol>
  );
}
