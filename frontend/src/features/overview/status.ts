import type { CycleOut, Overview } from "../../api/types";
import { formatRelativeSeconds } from "../../lib/time";
import { phaseTitle } from "../runs/ledger";

/** Web Connector contact age: recent under 15 minutes, late under 2 hours, silent after that. */
export const CONTACT_RECENT_SECONDS = 15 * 60;
export const CONTACT_SILENT_SECONDS = 2 * 60 * 60;
/** A snapshot is stale when it is older than the schedule interval plus this grace. */
export const SNAPSHOT_GRACE_HOURS = 6;

export type ContactKind = "recent" | "late" | "silent" | "never";

export function classifyContact(ageSeconds: number | null): ContactKind {
  if (ageSeconds === null) return "never";
  if (ageSeconds < CONTACT_RECENT_SECONDS) return "recent";
  return ageSeconds < CONTACT_SILENT_SECONDS ? "late" : "silent";
}

export const agoText = (ageSeconds: number) => formatRelativeSeconds(-ageSeconds);

export function isSnapshotStale(syncedAt: string | null, everyDays: number, nowMs: number): boolean {
  if (!syncedAt) return true;
  const limit = (everyDays * 24 + SNAPSHOT_GRACE_HOURS) * 3_600_000;
  return nowMs - new Date(syncedAt).getTime() > limit;
}

export interface Fmt {
  dateTime: (iso: string) => string;
  weekdayTime: (iso: string) => string;
}

function lastCycleSentence(c: CycleOut, fmt: Fmt): string {
  const at = fmt.dateTime(c.finished_at ?? c.started_at);
  switch (c.status) {
    case "done":
      return `Last sync finished ${at}, all done.`;
    case "dry_run_only":
      return `Last sync finished ${at}: it planned and checked everything but sent nothing.`;
    case "aborted":
      return `Last sync was aborted at ${at}.`;
    case "failed":
      return `Last sync failed at ${at}.`;
    case "interrupted":
      return `Last sync was interrupted at ${at}.`;
    default:
      return `Last sync ended ${at}.`;
  }
}

function nextRunSentence(o: Overview, fmt: Fmt): string {
  if (!o.scheduler.enabled) return "Automatic runs are switched off on the server.";
  if (!o.scheduler.schedule_enabled) return "Automatic runs are paused.";
  return o.scheduler.next_run_at ? `Next run ${fmt.weekdayTime(o.scheduler.next_run_at)}.` : "";
}

/** The one plain sentence at the top of the overview. */
export function statusSentence(o: Overview, fmt: Fmt): string {
  if (!o.database) return "The database is not answering, so nothing on this page can be trusted until it is back.";
  const running = o.running_cycle ?? o.running_push;
  if (running) {
    if (running.phase === "waiting_for_web_connector") {
      const ago = o.last_qbwc_contact_age_seconds;
      return `Waiting for the Web Connector. ${ago === null ? "It has never checked in" : `It last checked in ${agoText(ago)}`}; we can't hurry it.`;
    }
    const what = o.running_cycle ? "A sync" : "A push run";
    return running.phase ? `${what} is running: ${phaseTitle(running.phase)}.` : `${what} is running.`;
  }
  const next = nextRunSentence(o, fmt);
  if (!o.last_cycle) return ["No sync has run yet.", next].filter(Boolean).join(" ");
  return [lastCycleSentence(o.last_cycle, fmt), next].filter(Boolean).join(" ");
}
