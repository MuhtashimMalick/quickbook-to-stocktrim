import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { components } from "../api/schema";
import type { CycleOut, Overview } from "../api/types";
import { cycle, overview } from "./fixtures";

type TokenOut = components["schemas"]["TokenOut"];
type MeOut = components["schemas"]["MeOut"];
type SettingsOut = components["schemas"]["SettingsOut"];

export const server = setupServer();
/** Requests with no handler: the suite fails on any of them (see setup.ts). */
export const unhandled: string[] = [];

export const token = (access_token: string): TokenOut => ({ access_token, token_type: "bearer", expires_in: 900 });
export const me = (username = "ada"): MeOut => ({ username, last_login_at: null });
export const settings = (over: Partial<SettingsOut> = {}): SettingsOut => ({
  environment: "test",
  stocktrim_writes_enabled: false,
  scheduler_enabled: false,
  scheduler_allow_production: false,
  schedule: { enabled: false, every_days: 1, source: "env", time: "02:00", tz: "UTC" },
  sync_max_planned_calls: 2000,
  stocktrim_host: "api.example.test",
  stocktrim_max_rps: 5,
  read_jobs: [],
  sales_history_years: 3,
  negative_stock_policy: "as_is",
  alert_webhook_configured: false,
  history_retention_days: 90,
  version: null,
  users: [],
  ...over,
});

export const unauthorized = (detail: string) => HttpResponse.json({ detail }, { status: 401 });

/** What the overview and runs screens read on load: idle overview, one cycle, an empty reason catalog. */
export const liveData = (o: Overview = overview(), cycles: CycleOut[] = [cycle()]) => [
  http.get("*/api/v1/overview", () => HttpResponse.json(o)),
  http.get("*/api/v1/cycles", () => HttpResponse.json(cycles)),
  http.get("*/api/v1/reason-codes", () => HttpResponse.json({})),
];

/** Handlers for a signed-in session (refresh, me, settings, and the data the landing pages read). */
export const signedIn = (over: Partial<SettingsOut> = {}) => [
  http.post("*/api/v1/auth/refresh", () => HttpResponse.json(token("t1"))),
  http.get("*/api/v1/auth/me", () => HttpResponse.json(me())),
  http.get("*/api/v1/settings", () => HttpResponse.json(settings(over))),
  ...liveData(),
];

export { delay, http, HttpResponse };
