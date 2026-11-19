import { useState } from "react";

/** Polite live region for announcing changes (e.g. the new page after navigation) to screen readers. */
export function LiveRegion({ message }: { message: string }) {
  return (
    <div role="status" aria-live="polite" aria-atomic="true" className="sr-only">
      {message}
    </div>
  );
}

/**
 * The message to announce when a run's phase changes: silent on first render and while nothing changes,
 * "Now: <phase>" on a new phase, and `finished` when the phase disappears (the run ended).
 */
export function useRunAnnouncement(phase: string | null, finished: string): string {
  const [state, setState] = useState<{ phase: string | null; message: string }>({ phase, message: "" });
  if (state.phase !== phase) setState({ phase, message: phase ? `Now: ${phase}` : finished });
  return state.message;
}
