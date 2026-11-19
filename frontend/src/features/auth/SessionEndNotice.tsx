import { Clock } from "lucide-react";
import { useEffect, useState, useSyncExternalStore } from "react";
import { endSession, getSessionEnd, onSessionEnd } from "../../api/session";
import { Notice } from "../../components/Notice";
import { useFormatters } from "../../lib/useFormatters";

export const SESSION_WARNING_MS = 5 * 60_000;

/** Calm heads-up shortly before the 24 h session ends; at the absolute end the user is signed out like any other expiry. */
export function SessionEndNotice() {
  const end = useSyncExternalStore(onSessionEnd, getSessionEnd);
  const [warnFor, setWarnFor] = useState<number | null>(null);
  const f = useFormatters();

  useEffect(() => {
    if (end === null) return;
    const left = end - Date.now();
    const warn = setTimeout(() => setWarnFor(end), Math.max(0, left - SESSION_WARNING_MS));
    const stop = setTimeout(endSession, Math.max(0, left));
    return () => {
      clearTimeout(warn);
      clearTimeout(stop);
    };
  }, [end]);

  if (end === null || warnFor !== end) return null;
  return (
    <div role="status" className="mx-auto max-w-page px-6 pt-4">
      <Notice tone="warn" icon={Clock} title={`Your session ends at ${f.clock(end)}.`}>
        <p>Sign in again then; nothing is lost.</p>
      </Notice>
    </div>
  );
}
