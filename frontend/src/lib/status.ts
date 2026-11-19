import type { Status } from "../components/StatusStamp";

const MAP: Record<string, Status> = {
  running: "running",
  done: "done",
  done_with_failures: "partial",
  failed: "failed",
  aborted: "aborted",
  dry_run_only: "dry-run",
  interrupted: "interrupted",
  cancelled: "cancelled",
};

/** Cycle and push-run statuses (plain strings in the API) to a stamp; anything new shows as "Unknown" rather than as a wrong word. */
export const stampFor = (status: string): Status => MAP[status] ?? "unknown";

/** A push run that stopped early and may be resumed from the CLI (real runs only). */
export const RESUMABLE = ["interrupted", "cancelled", "failed", "done_with_failures"];
