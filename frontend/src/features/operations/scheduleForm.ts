import { ApiError } from "../../api/client";
import type { ScheduleIn, ScheduleOut } from "../../api/types";

export const FREQUENCIES = [
  { value: 1, label: "Every day" },
  { value: 3, label: "Every 3 days" },
  { value: 7, label: "Every week" },
] as const;

export type Frequency = (typeof FREQUENCIES)[number]["value"];

export interface ScheduleForm {
  enabled: boolean;
  every_days: Frequency;
  time: string;
  tz: string;
  /** yyyy-mm-dd, or "" for "today in the chosen zone". Only used for every 3 days / week. */
  start_date: string;
}

export type FieldErrors = Partial<Record<"time" | "tz" | "start_date" | "form", string>>;

/** The same pattern as the backend (HHMM): 24 h, two digits each, 00:00 to 23:59. */
export const TIME_PATTERN = /^([01][0-9]|2[0-3]):[0-5][0-9]$/;

export const toForm = (s: ScheduleOut): ScheduleForm => ({ enabled: s.enabled, every_days: s.every_days, time: s.time, tz: s.tz, start_date: s.start_date });

/** The start date means nothing for a daily schedule, so it never makes the form dirty or goes to the server then. */
const normalize = (f: ScheduleForm): ScheduleForm => (f.every_days === 1 ? { ...f, start_date: "" } : f);

export const isDirty = (draft: ScheduleForm, saved: ScheduleForm): boolean => JSON.stringify(normalize(draft)) !== JSON.stringify(normalize(saved));

export const validate = (f: ScheduleForm): FieldErrors => (TIME_PATTERN.test(f.time) ? {} : { time: "Use 24-hour HH:MM, from 00:00 to 23:59." });

export const toBody = (f: ScheduleForm): ScheduleIn => ({ enabled: f.enabled, every_days: f.every_days, time: f.time, tz: f.tz, start_date: normalize(f).start_date || null });

/** Every IANA zone the browser knows, plus UTC and the current value (so a stored zone the browser lacks still shows). */
export function timeZones(current: string): string[] {
  const known = typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  return [...new Set([...known, "UTC", current])].sort((a, b) => a.localeCompare(b));
}

const FIELD_MESSAGES = {
  time: "Use 24-hour HH:MM, from 00:00 to 23:59.",
  tz: "That time zone is not recognised. Pick one from the list.",
  start_date: "That is not a valid start date.",
};

const isField = (name: string): name is keyof typeof FIELD_MESSAGES => name in FIELD_MESSAGES;

/** Plain-language messages for a rejected save. `start_date_out_of_range` is the server's own code; 422 lists come from field validation. */
export function saveErrors(error: unknown): FieldErrors {
  if (!(error instanceof ApiError)) return { form: "The schedule could not be saved. Check the connection and try again." };
  if (error.detail === "start_date_out_of_range") return { start_date: "Pick a start date within one year of today." };
  if (error.status === 422 && Array.isArray(error.detail)) {
    const out: FieldErrors = {};
    for (const item of error.detail as { loc?: unknown[] }[]) {
      const field = String(item.loc?.at(-1) ?? "");
      if (isField(field)) out[field] = FIELD_MESSAGES[field];
      else out.form = "The server did not accept these values.";
    }
    return out;
  }
  return { form: "The schedule could not be saved. Try again in a moment." };
}
