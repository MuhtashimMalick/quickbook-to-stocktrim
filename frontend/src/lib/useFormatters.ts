import { useMemo } from "react";
import { useSettings } from "../features/settings/useSettings";
import { formatClock, formatClockSeconds, formatDateTime, formatWeekdayTime, validZone } from "./time";

/** Time formatters bound to the schedule's time zone from /settings (the browser's zone until it loads). */
export function useFormatters() {
  const tz = validZone(useSettings().data?.schedule.tz);
  return useMemo(
    () => ({
      tz,
      clock: (when: string | number) => formatClock(when, tz),
      clockSeconds: (when: string | number) => formatClockSeconds(when, tz),
      dateTime: (when: string | number) => formatDateTime(when, tz),
      weekdayTime: (when: string | number) => formatWeekdayTime(when, tz),
    }),
    [tz],
  );
}
