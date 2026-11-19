import { useNow } from "../lib/live";
import { formatRelative, formatUtc } from "../lib/time";
import { useFormatters } from "../lib/useFormatters";

/** A time in the schedule's zone (mono, tabular), with the UTC instant on hover. */
export function Time({ at, format = "dateTime" }: { at: string; format?: "dateTime" | "clock" | "weekdayTime" }) {
  const f = useFormatters();
  return (
    <time dateTime={at} title={formatUtc(at)} className="font-mono tabular-nums">
      {f[format](at)}
    </time>
  );
}

/** "14 minutes ago", re-rendered every 30 s; the exact time is on hover. */
export function Relative({ at }: { at: string }) {
  const now = useNow();
  return (
    <time dateTime={at} title={formatUtc(at)}>
      {formatRelative(at, now)}
    </time>
  );
}
