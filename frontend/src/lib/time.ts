/** Time formatting with Intl only (no date library). Zones are IANA names; an unknown zone falls back to the browser's. */
const LOCALE = "en-GB";

export function validZone(tz?: string | null): string | undefined {
  if (!tz) return undefined;
  try {
    new Intl.DateTimeFormat(LOCALE, { timeZone: tz });
    return tz;
  } catch {
    return undefined;
  }
}

function fmt(when: string | number, tz: string | undefined, opts: Intl.DateTimeFormatOptions): string {
  return new Intl.DateTimeFormat(LOCALE, { timeZone: validZone(tz), hourCycle: "h23", ...opts }).format(new Date(when));
}

const HM = { hour: "2-digit", minute: "2-digit" } as const;
export const formatClock = (when: string | number, tz?: string) => fmt(when, tz, HM);
export const formatClockSeconds = (when: string | number, tz?: string) => fmt(when, tz, { ...HM, second: "2-digit" });
export const formatDateTime = (when: string | number, tz?: string) => fmt(when, tz, { day: "numeric", month: "short", ...HM });
export const formatWeekdayTime = (when: string | number, tz?: string) => fmt(when, tz, { weekday: "short", ...HM });

/** The same instant in UTC, shown on hover. */
export const formatUtc = (when: string | number) => `${fmt(when, "UTC", { dateStyle: "medium", timeStyle: "medium" })} UTC`;

const RTF = new Intl.RelativeTimeFormat("en", { numeric: "auto" });
const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** "14 minutes ago" for a signed number of seconds (negative = past). */
export function formatRelativeSeconds(seconds: number): string {
  const abs = Math.abs(seconds);
  if (abs < 45) return RTF.format(0, "second");
  if (abs < HOUR) return RTF.format(Math.round(seconds / MINUTE), "minute");
  if (abs < DAY) return RTF.format(Math.round(seconds / HOUR), "hour");
  return RTF.format(Math.round(seconds / DAY), "day");
}

export const formatRelative = (when: string, nowMs: number) => formatRelativeSeconds(Math.round((new Date(when).getTime() - nowMs) / 1000));

/** "45s", "4m 12s", "2h 05m". */
export function formatDuration(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  if (s < MINUTE) return `${s}s`;
  if (s < HOUR) return `${Math.floor(s / MINUTE)}m ${String(s % MINUTE).padStart(2, "0")}s`;
  return `${Math.floor(s / HOUR)}h ${String(Math.floor((s % HOUR) / MINUTE)).padStart(2, "0")}m`;
}

export const durationBetween = (start: string, end: string | null, nowMs: number) => formatDuration((end ? new Date(end).getTime() : nowMs) - new Date(start).getTime());
