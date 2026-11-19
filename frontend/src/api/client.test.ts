import { describe, expect, it } from "vitest";
import { api, awaitRefresh, refreshSession } from "./client";
import { getSessionEnd, getToken, onSessionLost, setToken } from "./session";
import { delay, http, HttpResponse, server, settings, token, unauthorized } from "../test/server";

const settingsFor = (good: string) =>
  http.get("*/api/v1/settings", ({ request }) =>
    request.headers.get("Authorization") === `Bearer ${good}` ? HttpResponse.json(settings()) : unauthorized("token_expired"),
  );

function countRefreshes(respond: (n: number) => Response | Promise<Response>) {
  const counter = { n: 0 };
  server.use(
    http.post("*/api/v1/auth/refresh", () => {
      counter.n++;
      return respond(counter.n);
    }),
  );
  return counter;
}

describe("api client", () => {
  it("shares ONE refresh between concurrent 401s and retries successfully", async () => {
    const refreshes = countRefreshes(async () => {
      await delay(30);
      return HttpResponse.json(token("new"));
    });
    server.use(settingsFor("new"));
    setToken("old");
    const results = await Promise.all([1, 2, 3].map(() => api.GET("/api/v1/settings")));
    expect(refreshes.n).toBe(1);
    expect(results.every((r) => r.data?.environment === "test")).toBe(true);
    expect(getToken()).toBe("new");
  });

  it("3 concurrent 401s with a failing refresh: one refresh, all 401, token null, session-lost fires once", async () => {
    const refreshes = countRefreshes(async () => {
      await delay(30);
      return unauthorized("token_expired");
    });
    server.use(settingsFor("never"));
    let lost = 0;
    const off = onSessionLost(() => lost++);
    setToken("old");
    const results = await Promise.all([1, 2, 3].map(() => api.GET("/api/v1/settings")));
    off();
    expect(refreshes.n).toBe(1);
    expect(results.map((r) => r.response.status)).toEqual([401, 401, 401]);
    expect(getToken()).toBeNull();
    expect(lost).toBe(1);
  });

  it("a retried request that 401s again ends the session without looping", async () => {
    const refreshes = countRefreshes(() => HttpResponse.json(token("new")));
    server.use(settingsFor("never"));
    let lost = 0;
    const off = onSessionLost(() => lost++);
    setToken("old");
    const { response } = await api.GET("/api/v1/settings");
    off();
    expect(response.status).toBe(401);
    expect(refreshes.n).toBe(1);
    expect(lost).toBe(1);
    expect(getToken()).toBeNull();
  });

  it("a stale-token 401 after a finished refresh does not refresh again and retries with the new token", async () => {
    const refreshes = countRefreshes(() => HttpResponse.json(token("new")));
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    server.use(
      http.get("*/api/v1/settings", async ({ request }) => {
        if (request.headers.get("Authorization") === "Bearer new") return HttpResponse.json(settings());
        await gate; // this request answers 401 only after the refresh finished
        return unauthorized("token_expired");
      }),
    );
    setToken("old");
    const slow = api.GET("/api/v1/settings");
    await refreshSession(); // another caller refreshes meanwhile
    release();
    const { data } = await slow;
    expect(data?.environment).toBe("test");
    expect(refreshes.n).toBe(1);
  });

  it("retries a refresh_race once, after a short wait, then succeeds", async () => {
    const refreshes = countRefreshes((n) => (n === 1 ? unauthorized("refresh_race") : HttpResponse.json(token("t2"))));
    const started = Date.now();
    expect(await refreshSession()).toBe("ok");
    expect(refreshes.n).toBe(2);
    expect(Date.now() - started).toBeGreaterThanOrEqual(250); // waited before the single retry
  });

  it("gives up after the single retry", async () => {
    const refreshes = countRefreshes(() => unauthorized("refresh_race"));
    expect(await refreshSession()).toBe("rejected");
    expect(refreshes.n).toBe(2);
  });

  it.each(["invalid_refresh", "token_expired", "invalid_token"])("a 401 %s ends the session at once, with no retry", async (detail) => {
    const refreshes = countRefreshes(() => unauthorized(detail));
    expect(await refreshSession()).toBe("rejected");
    expect(refreshes.n).toBe(1);
  });

  it("stores session_expires_at from the refresh response and clears it with the token", async () => {
    countRefreshes(() => HttpResponse.json(token("t", "2026-11-20T10:00:00Z")));
    expect(await refreshSession()).toBe("ok");
    expect(getSessionEnd()).toBe(Date.parse("2026-11-20T10:00:00Z"));
    setToken(null);
    expect(getSessionEnd()).toBeNull();
  });

  it("does not retry other refresh failures", async () => {
    const refreshes = countRefreshes(() => HttpResponse.json({ detail: "rate_limited" }, { status: 429 }));
    expect(await refreshSession()).toBe("rejected");
    expect(refreshes.n).toBe(1);
  });

  it("a network error on refresh does not end the session", async () => {
    server.use(http.post("*/api/v1/auth/refresh", () => HttpResponse.error()), settingsFor("never"));
    let lost = 0;
    const off = onSessionLost(() => lost++);
    setToken("old");
    const { response } = await api.GET("/api/v1/settings");
    off();
    expect(response.status).toBe(401);
    expect(lost).toBe(0);
    expect(getToken()).toBe("old");
  });

  it("a rejected refresh ends the session and clears the token", async () => {
    server.use(settingsFor("never"), http.post("*/api/v1/auth/refresh", () => unauthorized("invalid_refresh")));
    let lost = 0;
    const off = onSessionLost(() => lost++);
    setToken("old");
    const { response } = await api.GET("/api/v1/settings");
    off();
    expect(response.status).toBe(401);
    expect(lost).toBe(1);
    expect(getToken()).toBeNull();
  });

  it("a failed login (401) never triggers a refresh", async () => {
    const refreshes = countRefreshes(() => HttpResponse.json(token("x")));
    server.use(http.post("*/api/v1/auth/login", () => unauthorized("invalid_credentials")));
    const { response } = await api.POST("/api/v1/auth/login", { body: { username: "a", password: "b" } });
    expect(response.status).toBe(401);
    expect(refreshes.n).toBe(0);
  });

  it("a refresh that finishes after the session was cleared does not revive it", async () => {
    countRefreshes(async () => {
      await delay(40);
      return HttpResponse.json(token("late"));
    });
    setToken("old");
    const pending = refreshSession();
    setToken(null); // logout while the refresh is in flight
    await pending;
    expect(getToken()).toBeNull();
  });

  it("awaitRefresh resolves once the in-flight refresh has settled", async () => {
    let done = false;
    countRefreshes(async () => {
      await delay(30);
      done = true;
      return HttpResponse.json(token("t"));
    });
    void refreshSession();
    await awaitRefresh();
    expect(done).toBe(true);
  });

  it("sends the Bearer token from memory", async () => {
    let auth: string | null = null;
    server.use(
      http.get("*/api/v1/settings", ({ request }) => {
        auth = request.headers.get("Authorization");
        return HttpResponse.json(settings());
      }),
    );
    setToken("abc");
    await api.GET("/api/v1/settings");
    expect(auth).toBe("Bearer abc");
  });
});
