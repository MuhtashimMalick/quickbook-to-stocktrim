import { describe, expect, it } from "vitest";

// Every design value lives in src/styles/tokens.css. Component sources must not carry raw colours, pixel/rem sizes or inline styles.
const sources = import.meta.glob(["../**/*.{ts,tsx}", "!../**/*.test.{ts,tsx}", "!../test/**", "!../api/schema.d.ts"], { query: "?raw", import: "default", eager: true }) as Record<string, string>;

const RULES: [string, RegExp][] = [
  ["raw hex colour", /["'`]#[0-9a-fA-F]{3,8}["'`]/],
  ["rgb()/hsl() colour", /\b(?:rgba?|hsla?)\(/],
  ["arbitrary px/rem/em size", /\[-?[\d.]+(?:px|rem|em)\]/],
  ["inline style", /\bstyle=\{\{/],
];

describe("design tokens", () => {
  it("scans the app sources", () => {
    expect(Object.keys(sources).length).toBeGreaterThan(40);
  });

  it.each(RULES)("no %s outside tokens.css", (_name, pattern) => {
    const offenders = Object.entries(sources)
      .filter(([, text]) => pattern.test(text))
      .map(([file]) => file);
    expect(offenders).toEqual([]);
  });
});
