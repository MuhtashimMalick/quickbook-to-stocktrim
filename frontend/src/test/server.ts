import { delay, http, HttpResponse } from "msw";
import { setupServer } from "msw/node";
import type { components } from "../api/schema";

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

/** Handlers for a signed-in session (refresh, me, settings). */
export const signedIn = (over: Partial<SettingsOut> = {}) => [
  http.post("*/api/v1/auth/refresh", () => HttpResponse.json(token("t1"))),
  http.get("*/api/v1/auth/me", () => HttpResponse.json(me())),
  http.get("*/api/v1/settings", () => HttpResponse.json(settings(over))),
];

export { delay, http, HttpResponse };
