import { ICON_SIZE } from "../lib/icons";
import { CircleHelp, CircleSlash, PencilLine, TriangleAlert } from "lucide-react";
import { useEnvironment } from "../features/settings/useEnvironment";
import { useSettings } from "../features/settings/useSettings";

/** PRODUCTION: full-width ink banner with hazard stripes. TEST: the thin rule + tag live in the shell. */
export function ProductionBanner() {
  if (useEnvironment() !== "production") return null;
  return (
    <div role="region" aria-label="Environment" className="bg-ink text-accent-ink">
      <div className="hazard-stripes h-1" aria-hidden="true" />
      <p className="flex items-center justify-center gap-2 px-4 py-2 text-center font-semibold uppercase tracking-wide">
        <TriangleAlert size={ICON_SIZE} aria-hidden="true" className="text-hazard" />
        PRODUCTION - writes go to the real StockTrim account
      </p>
      <div className="hazard-stripes h-1" aria-hidden="true" />
    </div>
  );
}

export function EnvTag() {
  const env = useEnvironment();
  if (env === "unknown") {
    return (
      <span className="inline-flex items-center gap-1 rounded-sm border border-dashed border-aborted bg-aborted-tint px-2 py-0.5 text-table text-aborted">
        <CircleHelp size={ICON_SIZE} aria-hidden="true" />
        Environment: unknown
      </span>
    );
  }
  const production = env === "production";
  return (
    <span className={`rounded-sm px-2 py-0.5 font-mono text-table font-medium uppercase ${production ? "bg-ink text-accent-ink" : "border border-rule text-ink-muted"}`}>
      {production ? "Production" : "TEST"}
    </span>
  );
}

/** Solid when writes are enabled, dashed when they are not; always icon + words. */
export function WritesChip() {
  const { data, isError } = useSettings();
  if (!data && !isError) return null;
  const enabled = data?.stocktrim_writes_enabled === true;
  const Icon = enabled ? PencilLine : CircleSlash;
  const text = !data ? "Writes: unknown" : enabled ? "Writes: ENABLED" : "Writes: DISABLED (dry-run only)";
  return (
    <span
      title="set on the server"
      className={`inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 text-table ${enabled ? "border-solid border-ink bg-ink text-accent-ink" : "border-dashed border-dry-run bg-dry-run-tint text-dry-run"}`}
    >
      <Icon size={ICON_SIZE} aria-hidden="true" />
      <span>{text}</span>
      <span className="font-normal opacity-80">(set on the server)</span>
    </span>
  );
}
