import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { StatusStamp, STATUSES } from "./StatusStamp";

describe("StatusStamp", () => {
  it.each(STATUSES)("renders an icon and a word for %s", (status) => {
    const { container } = render(<StatusStamp status={status} />);
    expect(container.querySelector("svg")).toBeInTheDocument();
    expect(container.textContent?.trim().length).toBeGreaterThan(0);
  });

  it("covers all eleven statuses and draws dry run dashed", () => {
    expect(STATUSES).toHaveLength(11);
    render(<StatusStamp status="dry-run" />);
    expect(screen.getByText("Dry run").className).toContain("border-dashed");
  });
});
