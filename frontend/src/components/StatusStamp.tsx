import { useRef } from "react";
import { ICON_SIZE } from "../lib/icons";
import { useStampPress } from "../lib/motion";
import { Ban, CircleAlert, CircleCheck, CircleHelp, CirclePause, CircleSlash, CircleX, FlaskConical, LoaderCircle, SkipForward, TriangleAlert, type LucideIcon } from "lucide-react";

// Full class strings so Tailwind can see them. Every status shows icon + word + colour, never colour alone.
export type Status = "done" | "failed" | "skipped" | "excluded" | "running" | "aborted" | "dry-run" | "interrupted" | "cancelled" | "partial" | "unknown";

const STATUS: Record<Status, { label: string; icon: LucideIcon; classes: string }> = {
  done: { label: "Done", icon: CircleCheck, classes: "border-done bg-done-tint text-done" },
  failed: { label: "Failed", icon: CircleX, classes: "border-failed bg-failed-tint text-failed" },
  skipped: { label: "Skipped", icon: SkipForward, classes: "border-skipped bg-skipped-tint text-skipped" },
  excluded: { label: "Excluded", icon: Ban, classes: "border-excluded bg-excluded-tint text-excluded" },
  running: { label: "Running", icon: LoaderCircle, classes: "border-running bg-running-tint text-running" },
  aborted: { label: "Aborted", icon: TriangleAlert, classes: "border-aborted bg-aborted-tint text-aborted" },
  "dry-run": { label: "Dry run", icon: FlaskConical, classes: "border-dashed border-dry-run bg-dry-run-tint text-dry-run" },
  interrupted: { label: "Interrupted", icon: CirclePause, classes: "border-aborted bg-aborted-tint text-aborted" },
  cancelled: { label: "Cancelled", icon: CircleSlash, classes: "border-skipped bg-skipped-tint text-skipped" },
  partial: { label: "Done with failures", icon: CircleAlert, classes: "border-aborted bg-aborted-tint text-aborted" },
  unknown: { label: "Unknown", icon: CircleHelp, classes: "border-dashed border-skipped bg-skipped-tint text-skipped" },
};

export const STATUSES = Object.keys(STATUS) as Status[];

/** Rectangular status stamp. `label` swaps the word (for example "Late") while the icon and colour of the status stay. */
export function StatusStamp({ status, label }: { status: Status; label?: string }) {
  const { label: defaultLabel, icon: Icon, classes } = STATUS[status];
  const word = label ?? defaultLabel;
  const ref = useRef<HTMLSpanElement>(null);
  useStampPress(ref, status + word);
  return (
    <span ref={ref} className={`inline-flex items-center gap-1 rounded-sm border px-2 py-0.5 text-table font-semibold uppercase tracking-wide ${classes}`}>
      <Icon size={ICON_SIZE} aria-hidden="true" />
      {word}
    </span>
  );
}
