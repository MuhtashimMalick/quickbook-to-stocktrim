import { useEffect, useRef, type ReactNode } from "react";
import { useNavigationType } from "react-router";
import { useRouteEnter } from "../lib/motion";

/** One h1 per page, a one-line purpose, and the route-enter motion on its children. After in-app navigation the h1 takes focus. */
export function Page({ title, purpose, children }: { title: string; purpose: string; children?: ReactNode }) {
  const ref = useRef<HTMLDivElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const navigated = useNavigationType() !== "POP";
  useRouteEnter(ref);
  useEffect(() => {
    document.title = `${title} · QuickBook`;
  }, [title]);
  useEffect(() => {
    if (navigated) heading.current?.focus({ preventScroll: true });
  }, [navigated]);
  return (
    <div ref={ref} className="space-y-6">
      <div>
        <h1 ref={heading} tabIndex={-1} className="text-title outline-none">
          {title}
        </h1>
        <p className="mt-1 text-ink-muted">{purpose}</p>
      </div>
      {children}
    </div>
  );
}
