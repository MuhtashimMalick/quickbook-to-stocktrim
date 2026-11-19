import { Mono } from "../../components/Mono";

const ORDER = ["done", "failed", "skipped", "pending"];

/** Item counts by status ("done 12, failed 1"), in a fixed order; zero counts are left out. */
export function Counts({ counts }: { counts: Record<string, number> | null | undefined }) {
  const entries = Object.entries(counts ?? {})
    .filter(([, n]) => n > 0)
    .sort(([a], [b]) => (ORDER.indexOf(a) + 1 || ORDER.length + 1) - (ORDER.indexOf(b) + 1 || ORDER.length + 1));
  if (entries.length === 0) return <span className="text-ink-muted">none</span>;
  return (
    <span className="flex flex-wrap gap-x-3">
      {entries.map(([status, n]) => (
        <span key={status}>
          {status} <Mono>{n.toLocaleString("en-GB")}</Mono>
        </span>
      ))}
    </span>
  );
}
