import type { PushRunOut } from "../../api/types";
import { Mono } from "../../components/Mono";
import { ProgressBar } from "../../components/ProgressBar";
import { formatDuration } from "../../lib/time";
import { useReasonTitle } from "./api";

const sum = (c: Record<string, number>) => Object.values(c).reduce((a, b) => a + b, 0);

/** Items sent for one entity out of all its items (done + failed + skipped + pending). */
export function EntityProgress({ entity, counts, errors }: { entity: string; counts: Record<string, number>; errors?: Record<string, number> }) {
  const title = useReasonTitle();
  const total = sum(counts);
  const done = counts.done ?? 0;
  return (
    <li className="space-y-1">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4">
        <span className="font-medium">{entity}</span>
        <span className="text-table text-ink-muted">
          <Mono>{done.toLocaleString("en-GB")}</Mono> of <Mono>{total.toLocaleString("en-GB")}</Mono> done
        </span>
      </div>
      <ProgressBar value={done} max={total} label={`${entity} progress`} />
      <p className="text-table text-ink-muted">
        failed <Mono>{counts.failed ?? 0}</Mono> · skipped <Mono>{counts.skipped ?? 0}</Mono> · pending <Mono>{counts.pending ?? 0}</Mono>
      </p>
      {errors && Object.keys(errors).length > 0 && (
        <ul className="text-table text-failed">
          {Object.entries(errors).map(([code, n]) => (
            <li key={code}>
              {title(code)} <Mono>x{n}</Mono>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

/** Per-entity progress rows of a push run, with the ETA while it runs. */
export function RunProgress({ run }: { run: PushRunOut }) {
  const entities = Object.keys(run.counts);
  if (entities.length === 0) return <p className="text-ink-muted">No items have been planned for this run.</p>;
  return (
    <div className="space-y-3">
      <ul className="space-y-4">
        {entities.map((e) => (
          <EntityProgress key={e} entity={e} counts={run.counts[e] ?? {}} errors={run.error_codes[e]} />
        ))}
      </ul>
      {run.status === "running" && run.eta_seconds !== null && <p className="text-table text-ink-muted">About {formatDuration(run.eta_seconds * 1000)} left at the current rate.</p>}
    </div>
  );
}
