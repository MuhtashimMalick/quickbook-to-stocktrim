# Design decisions: "Dispatch Ledger" (light theme only)

Warm paper, ruled hairlines, rectangular status stamps, serif display headings, mono identifiers.
All values live in `src/styles/tokens.css` (Tailwind v4 `@theme`); components use only token utilities.

- Colour: paper `#F6F3EC`, sheet `#FFFDF8`, rule `#DDD6C6`, ink `#1D1B16`, ink-muted `#5E5A4E`, accent (fletch green) `#1F5C4A`, hazard `#E0A100`.
- Status (text/tint): done, failed, skipped, excluded, running, aborted, dry-run (dashed border). A status always shows icon + word + colour (`StatusStamp`).
- Type: Newsreader 600 (headings and the QuickBook wordmark only), IBM Plex Sans (UI, 15 px body, 13 px tables), IBM Plex Mono with tabular numbers (`Mono`) for ids, counts, times. Self-hosted via @fontsource, latin subsets only.
- Shape: 4 px spacing scale, radii 2/4 px, no card shadows (rules + paper/sheet contrast); only overlays use `shadow-overlay`. 36 px rows (`h-row`). Lucide icons at 16 px.
- Environment: TEST = thin accent top rule + `TEST` tag. PRODUCTION = ink banner with hazard stripes and a warmer paper (`[data-env="production"]`). The writes chip (solid/dashed) reads GET /settings.
- Motion (GSAP, `src/lib/motion.ts`): route enter = fade + 8 px rise, 0.25 s, stagger 0.05. Registered inside `gsap.matchMedia()` for `prefers-reduced-motion: no-preference`, so reduced motion shows the final state at once. No loops.
- Accessibility: skip link, landmarks, one h1 per page, 2 px accent focus ring, polite live region (`LiveRegion`) announcing the page on navigation, login error receives focus.
