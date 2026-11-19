import { render, screen } from "@testing-library/react";
import { lazy } from "react";
import { describe, expect, it, vi } from "vitest";
import { RouteErrorBoundary } from "./RouteErrorBoundary";

describe("RouteErrorBoundary", () => {
  it("shows the reload state when a lazy chunk fails to load", async () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    const Broken = lazy(() => Promise.reject(new Error("chunk load failed")));
    render(
      <RouteErrorBoundary>
        <Broken />
      </RouteErrorBoundary>,
    );
    expect(await screen.findByText("The app was updated or a page failed to load.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reload" })).toBeInTheDocument();
    vi.restoreAllMocks();
  });
});
