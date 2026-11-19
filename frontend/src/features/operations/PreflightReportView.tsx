import { TriangleAlert } from "lucide-react";
import { useState } from "react";
import type { Finding, PreflightReport } from "../../api/types";
import { Button } from "../../components/Button";
import { Mono } from "../../components/Mono";
import { Notice } from "../../components/Notice";
import { StatusStamp } from "../../components/StatusStamp";
import { DataTable, RowTh, Td, Th } from "../../components/Table";
import { formatDuration } from "../../lib/time";
import { useReasonCodes } from "../runs/api";

const COLLAPSED = 5;

export interface Row {
  finding: Finding;
  blocking: boolean;
}

/** Blocking findings first, then warnings; the API's order is kept inside each group. */
export const orderFindings = (v: PreflightReport["verdict"]): Row[] => [
  ...v.blocking.map((finding) => ({ finding, blocking: true })),
  ...v.warnings.map((finding) => ({ finding, blocking: false })),
];

function FindingItem({ row }: { row: Row }) {
  const { data } = useReasonCodes();
  const reason = data?.[row.finding.code];
  const detail = Object.entries(row.finding.detail ?? {});
  return (
    <li className="space-y-1 py-3">
      <div className="flex flex-wrap items-center gap-2">
        <StatusStamp status={row.blocking ? "failed" : "aborted"} label={row.blocking ? "Blocking" : "Warning"} />
        <span className="font-semibold">{reason?.title ?? row.finding.code}</span>
        {reason && <Mono className="text-table text-ink-muted">{row.finding.code}</Mono>}
        <span className="text-ink-muted">
          Count <Mono>{row.finding.count}</Mono>
        </span>
      </div>
      {reason ? (
        <>
          <p>{reason.explanation}</p>
          <p className="text-ink-muted">Next: {reason.next_step}</p>
        </>
      ) : (
        <p className="text-ink-muted">This code has no description yet. Give it to support as shown.</p>
      )}
      {detail.length > 0 && (
        <p className="text-table text-ink-muted">
          {detail.map(([key, n], i) => (
            <span key={key}>
              {i > 0 && ", "}
              <Mono>
                {key}: {n}
              </Mono>
            </span>
          ))}
        </p>
      )}
    </li>
  );
}

function Findings({ rows }: { rows: Row[] }) {
  const [all, setAll] = useState(false);
  const shown = all ? rows : rows.slice(0, COLLAPSED);
  return (
    <div>
      <ul className="divide-y divide-rule border-y border-rule" aria-label="Findings, blocking first">
        {shown.map((row) => (
          <FindingItem key={`${row.blocking}-${row.finding.code}`} row={row} />
        ))}
      </ul>
      {rows.length > COLLAPSED && (
        <Button variant="secondary" className="mt-3" aria-expanded={all} onClick={() => setAll(!all)}>
          {all ? "Show fewer" : `Show all ${rows.length}`}
        </Button>
      )}
    </div>
  );
}

const ENTITIES = ["products", "suppliers", "customers"] as const;

export function PreflightReportView({ report }: { report: PreflightReport }) {
  const rows = orderFindings(report.verdict);
  const { quickbooks, stocktrim, plan, verdict } = report;
  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <StatusStamp status={verdict.ok_to_push ? "done" : "failed"} label={verdict.ok_to_push ? "OK to push" : "Blocked"} />
        <span>
          {verdict.blocking.length} blocking, {verdict.warnings.length} {verdict.warnings.length === 1 ? "warning" : "warnings"}.
        </span>
      </div>
      {stocktrim.status !== "ok" && (
        <Notice tone="warn" icon={TriangleAlert} title="The StockTrim part of the check did not finish.">
          <p>
            Status <Mono>{stocktrim.status}</Mono>
            {stocktrim.error && (
              <>
                , code <Mono>{stocktrim.error}</Mono>
              </>
            )}
            .
          </p>
        </Notice>
      )}
      {rows.length === 0 ? <p>No findings. Everything the check looks at is in order.</p> : <Findings rows={rows} />}
      <DataTable caption="QuickBooks records by entity">
        <thead>
          <tr>
            <Th>Entity</Th>
            <Th align="right">Read</Th>
            <Th align="right">Ready</Th>
            <Th align="right">Set aside</Th>
            <Th align="right">Unreadable</Th>
            <Th align="right">Warnings</Th>
          </tr>
        </thead>
        <tbody>
          {ENTITIES.map((name) => {
            const c = quickbooks.entities[name];
            return (
              <tr key={name}>
                <RowTh>{name[0]?.toUpperCase() + name.slice(1)}</RowTh>
                {[c.parsed, c.ok, c.quarantined, c.parse_errors, c.warnings].map((n, i) => (
                  <Td key={i} align="right">
                    <Mono>{n}</Mono>
                  </Td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </DataTable>
      <p className="text-ink-muted">
        A later push would make at most <Mono>{plan.total_calls}</Mono> calls, about <Mono>{formatDuration(plan.eta_seconds * 1000)}</Mono> at <Mono>{plan.rps}</Mono> requests per second.
      </p>
    </div>
  );
}
