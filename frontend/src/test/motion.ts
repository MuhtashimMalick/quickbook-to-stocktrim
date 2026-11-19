/** jsdom has no matchMedia: report either the reduced-motion or the no-preference query as matching. */
export function mockMotion(mode: "reduce" | "no-preference"): void {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: (query: string): MediaQueryList => ({
      matches: query.includes(`prefers-reduced-motion: ${mode}`),
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}
