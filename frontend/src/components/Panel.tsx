import type { ReactNode } from "react";

/** A ruled sheet. No shadow: separation comes from the rule and the paper/sheet contrast. */
export function Panel({ title, children, className = "" }: { title?: string; children: ReactNode; className?: string }) {
  return (
    <section className={`border border-rule bg-sheet ${className}`}>
      {title && (
        <h2 className="border-b border-rule px-4 py-3 text-heading">{title}</h2>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}
