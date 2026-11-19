import type { Overview, ReasonOut, ScheduleOut } from "../api/types";
import { overview, scheduleOut } from "./fixtures";
import { renderApp } from "./render";
import { http, HttpResponse, liveData, server, signedIn } from "./server";

interface Opts {
  schedule?: ScheduleOut;
  overview?: Overview;
  settings?: Parameters<typeof signedIn>[0];
  catalog?: Record<string, ReasonOut>;
}

/** The Operations screen with the handlers it reads on load (schedule, overview, settings, catalog); tests add their POST/PUT handlers first. */
export function openOperations(o: Opts = {}) {
  server.use(
    http.get("*/api/v1/schedule", () => HttpResponse.json(o.schedule ?? scheduleOut())),
    http.get("*/api/v1/reason-codes", () => HttpResponse.json(o.catalog ?? {})),
    ...liveData(o.overview ?? overview()),
    ...signedIn(o.settings),
  );
  return renderApp("/operations");
}
