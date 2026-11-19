import type { CycleOut } from "../../api/types";
import { StatusStamp } from "../../components/StatusStamp";
import { DataTable, Td, Th } from "../../components/Table";
import { Time } from "../../components/Time";
import { TextLink } from "../../components/TextLink";
import { Mono } from "../../components/Mono";
import { useNow } from "../../lib/live";
import { stampFor } from "../../lib/status";
import { durationBetween } from "../../lib/time";
import { useReasonTitle } from "./api";

/** Cycles, newest first. `compact` drops the push-run column (used on the overview). */
export function CycleTable({ cycles, compact = false, caption = "Sync cycles, newest first" }: { cycles: CycleOut[]; compact?: boolean; caption?: string }) {
  const now = useNow();
  const title = useReasonTitle();
  return (
    <DataTable caption={caption}>
      <thead>
        <tr>
          <Th>Started</Th>
          <Th>Status</Th>
          <Th>Trigger</Th>
          <Th>Duration</Th>
          <Th>Reason</Th>
          {!compact && <Th>Push run</Th>}
        </tr>
      </thead>
      <tbody>
        {cycles.map((c) => (
          <tr key={c.id}>
            <Td>
              <TextLink to={`/runs/${c.id}`}>
                <Time at={c.started_at} />
              </TextLink>
            </Td>
            <Td>
              <StatusStamp status={stampFor(c.status)} />
            </Td>
            <Td>{c.trigger}</Td>
            <Td>
              <Mono>{durationBetween(c.started_at, c.finished_at, now)}</Mono>
            </Td>
            <Td>{c.reason ? title(c.reason) : <span className="text-ink-muted">none</span>}</Td>
            {!compact && (
              <Td>
                {c.push_run_id ? (
                  <TextLink to={`/runs/push/${c.push_run_id}`}>
                    <Mono>{c.push_run_id.slice(0, 8)}</Mono>
                  </TextLink>
                ) : (
                  <span className="text-ink-muted">none</span>
                )}
              </Td>
            )}
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}
