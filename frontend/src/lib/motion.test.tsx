import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Page } from "../components/Page";
import { mockMotion } from "../test/motion";

const container = () => screen.getByText("Runs").parentElement as HTMLElement;

describe("route enter motion", () => {
  it("reduced motion: renders the final state immediately", () => {
    mockMotion("reduce");
    render(<Page title="Runs" purpose="p" />);
    expect(container().style.visibility).toBe("");
    expect(container().style.opacity).toBe("");
  });

  it("no preference: children start from the hidden state and animate in", () => {
    mockMotion("no-preference");
    render(<Page title="Runs" purpose="p" />);
    expect(container().style.visibility).toBe("hidden");
  });
});
