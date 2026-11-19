import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { getToken } from "../../api/session";
import { renderApp } from "../../test/render";
import { delay, http, HttpResponse, me, server, liveData, settings, signedIn, token, unauthorized } from "../../test/server";

const where = () => screen.getByTestId("location").textContent;
const noSession = http.post("*/api/v1/auth/refresh", () => unauthorized("invalid_refresh"));
const afterLogin = [
  http.get("*/api/v1/auth/me", () => HttpResponse.json(me())),
  http.get("*/api/v1/settings", () => HttpResponse.json(settings())),
  ...liveData(),
];

async function signIn(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText("Username"), "ada");
  await user.type(screen.getByLabelText("Password"), "correct horse battery");
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("route guard", () => {
  it("sends an anonymous visitor to /login with a next parameter", async () => {
    server.use(noSession);
    renderApp("/runs?status=failed");
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(where()).toBe("/login?next=%2Fruns%3Fstatus%3Dfailed");
  });

  it("restores a session silently on load", async () => {
    server.use(...signedIn());
    renderApp("/runs");
    expect(await screen.findByRole("heading", { level: 1, name: "Runs" })).toBeInTheDocument();
  });

  it("goes back to the login page, with a calm notice, when the session is lost", async () => {
    let refreshes = 0;
    server.use(
      http.post("*/api/v1/auth/refresh", () => (++refreshes === 1 ? HttpResponse.json(token("t1")) : unauthorized("invalid_refresh"))),
      http.get("*/api/v1/auth/me", () => HttpResponse.json(me())),
      http.get("*/api/v1/settings", () => unauthorized("token_expired")),
      ...liveData(),
    );
    renderApp("/");
    expect(await screen.findByText("You are signed out. Sign in again.")).toBeInTheDocument();
    expect(getToken()).toBeNull();
  });
});

describe("login", () => {
  it("signs in and goes to the requested page", async () => {
    server.use(noSession, http.post("*/api/v1/auth/login", () => HttpResponse.json(token("t1"))), ...afterLogin);
    renderApp("/login?next=%2Fruns");
    await signIn(userEvent.setup());
    expect(await screen.findByRole("heading", { level: 1, name: "Runs" })).toBeInTheDocument();
    expect(getToken()).toBe("t1");
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });

  it.each(["https://evil.example/x", "//evil.example"])("ignores the unsafe next %s", async (next) => {
    server.use(noSession, http.post("*/api/v1/auth/login", () => HttpResponse.json(token("t1"))), ...afterLogin);
    renderApp(`/login?next=${encodeURIComponent(next)}`);
    await signIn(userEvent.setup());
    expect(await screen.findByRole("heading", { level: 1, name: "Overview" })).toBeInTheDocument();
    expect(where()).toBe("/");
  });

  it("shows the exact invalid/locked message and moves focus to it", async () => {
    server.use(noSession, http.post("*/api/v1/auth/login", () => unauthorized("invalid_credentials")));
    renderApp("/login");
    await signIn(userEvent.setup());
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Invalid credentials, or too many attempts: wait 15 minutes");
    expect(alert).toHaveFocus();
  });

  it("shows the Retry-After seconds on 429", async () => {
    server.use(
      noSession,
      http.post("*/api/v1/auth/login", () =>
        HttpResponse.json({ detail: "rate_limited" }, { status: 429, headers: { "Retry-After": "42" } }),
      ),
    );
    renderApp("/login");
    await signIn(userEvent.setup());
    expect(await screen.findByRole("alert")).toHaveTextContent("Try again in 42 seconds");
  });

  it("disables the button while submitting", async () => {
    server.use(
      noSession,
      http.post("*/api/v1/auth/login", async () => {
        await delay(50);
        return unauthorized("invalid_credentials");
      }),
    );
    renderApp("/login");
    await signIn(userEvent.setup());
    expect(await screen.findByRole("button", { name: "Signing in…" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Sign in" })).toBeEnabled());
  });
});

describe("logout", () => {
  it("calls /auth/logout, clears the token and leaves nothing in web storage", async () => {
    let loggedOut = 0;
    server.use(
      ...signedIn(),
      http.post("*/api/v1/auth/logout", () => {
        loggedOut++;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/");
    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: /ada/ }));
    await user.click(screen.getByRole("button", { name: "Sign out" }));
    expect(await screen.findByRole("heading", { level: 1, name: "Sign in" })).toBeInTheDocument();
    expect(loggedOut).toBe(1);
    expect(getToken()).toBeNull();
    expect(localStorage.length + sessionStorage.length).toBe(0);
  });
});
