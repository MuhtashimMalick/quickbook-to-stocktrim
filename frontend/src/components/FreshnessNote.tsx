import { CloudOff } from "lucide-react";
import { STALE_AFTER_FAILURES } from "../lib/live";
import { useFormatters } from "../lib/useFormatters";
import { Mono } from "./Mono";
import { Notice } from "./Notice";

/** "Last updated HH:MM:SS"; after three failed refreshes in a row it says plainly that the page is no longer live. */
export function FreshnessNote({ updatedAt, failures, onRetry }: { updatedAt: number; failures: number; onRetry?: () => void }) {
  const f = useFormatters();
  if (!updatedAt) return null;
  const last = <Mono>{f.clockSeconds(updatedAt)}</Mono>;
  if (failures >= STALE_AFTER_FAILURES) {
    return (
      <div role="alert">
        <Notice tone="warn" icon={CloudOff} title="Live updates have stopped">
          <p>
            The last {failures} refreshes failed, so this page may be out of date. Showing data from {last}.
          </p>
          {onRetry && (
            <button type="button" onClick={onRetry} className="cursor-pointer font-medium text-accent underline underline-offset-2">
              Refresh now
            </button>
          )}
        </Notice>
      </div>
    );
  }
  return <p className="text-table text-ink-muted">Last updated {last}</p>;
}
