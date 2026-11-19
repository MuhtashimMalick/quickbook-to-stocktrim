import type { ReactNode } from "react";

/** A real table: caption for assistive tech, scrolls sideways inside its panel on a narrow screen. */
export function DataTable({ caption, children }: { caption: string; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-table">
        <caption className="sr-only">{caption}</caption>
        {children}
      </table>
    </div>
  );
}

export function Th({ children, align = "left" }: { children?: ReactNode; align?: "left" | "right" }) {
  return (
    <th scope="col" className={`whitespace-nowrap border-b border-rule px-3 py-2 font-semibold text-ink-muted ${align === "right" ? "text-right" : ""}`}>
      {children}
    </th>
  );
}

/** First cell of a row: the row's header. */
export function RowTh({ children }: { children: ReactNode }) {
  return (
    <th scope="row" className="border-b border-rule px-3 py-2 text-left font-medium">
      {children}
    </th>
  );
}

export function Td({ children, align = "left", className = "" }: { children?: ReactNode; align?: "left" | "right"; className?: string }) {
  return <td className={`border-b border-rule px-3 py-2 align-top ${align === "right" ? "text-right" : ""} ${className}`}>{children}</td>;
}
