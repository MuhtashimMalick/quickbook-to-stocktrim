import { useEffect, useRef, type ReactNode } from "react";
import { useRouteEnter } from "../lib/motion";

/** One h1 per page, a one-line purpose, and the route-enter motion on its children. */
export function Page({ title, purpose, children }: { title: string; purpose: string; children?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  useRouteEnter(ref);
  useEffect(() => {
    document.title = `${title} · QuickBook`;
  }, [title]);
  return (
    <div ref={ref} className="space-y-6">
      <div>
        <h1 className="text-title">{title}</h1>
        <p className="mt-1 text-ink-muted">{purpose}</p>
      </div>
      {children}
    </div>
  );
}
