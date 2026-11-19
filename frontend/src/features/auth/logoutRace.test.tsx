import { screen } from "@testing-library/react";
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";
import { Providers } from "../../app/providers";
import { getToken } from "../../api/session";
import { delay, http, HttpResponse, me, server, token } from "../../test/server";
import { useAuth } from "./AuthProvider";

function Probe() {
  const { state, logout } = useAuth();
  return (
    <button type="button" onClick={() => void logout()}>
      logout ({state.status})
    </button>
  );
}

describe("logout during an in-flight refresh", () => {
  it("sends the logout after the refresh settles and leaves the session cleared", async () => {
    const events: string[] = [];
    server.use(
      http.post("*/api/v1/auth/refresh", async () => {
        await delay(80);
        events.push("refresh");
        return HttpResponse.json(token("late"));
      }),
      http.get("*/api/v1/auth/me", () => HttpResponse.json(me())),
      http.post("*/api/v1/auth/logout", () => {
        events.push("logout");
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const Router = ({ children }: { children: React.ReactNode }) => <MemoryRouter>{children}</MemoryRouter>;
    render(
      <Providers Router={Router}>
        <Probe />
      </Providers>,
    );
    await userEvent.setup().click(screen.getByRole("button")); // the silent refresh on load is still in flight
    await screen.findByRole("button", { name: "logout (anonymous)" });
    expect(events).toEqual(["refresh", "logout"]);
    expect(getToken()).toBeNull();
  });
});
