import type { ReactNode } from "react";

/** Identifiers, counts and times: IBM Plex Mono with tabular numbers. */
export function Mono({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <span className={`font-mono tabular-nums ${className}`}>{children}</span>;
}
