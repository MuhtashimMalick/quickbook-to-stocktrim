import type { RunListItem } from "../../api/types";
import { Mono } from "../../components/Mono";
import { StatusStamp } from "../../components/StatusStamp";
import { DataTable, Td, Th } from "../../components/Table";
import { TextLink } from "../../components/TextLink";
import { Time } from "../../components/Time";
import { stampFor } from "../../lib/status";
import { Counts } from "./Counts";

/** Push runs, newest first: the dry-run stamp sits next to the status so a dry run is never mistaken for a real one. */
export function PushRunTable({ runs }: { runs: RunListItem[] }) {
  return (
    <DataTable caption="Push runs, newest first">
      <thead>
        <tr>
          <Th>Started</Th>
          <Th>Status</Th>
          <Th>Mode</Th>
          <Th>Items</Th>
          <Th>Requested by</Th>
        </tr>
      </thead>
      <tbody>
        {runs.map((r) => (
          <tr key={r.id}>
            <Td>
              <TextLink to={`/runs/push/${r.id}`}>
                <Time at={r.started_at} />
              </TextLink>
            </Td>
            <Td>
              <span className="flex flex-wrap gap-1">
                {r.dry_run && <StatusStamp status="dry-run" />}
                <StatusStamp status={stampFor(r.status)} />
              </span>
            </Td>
            <Td>{r.mode ?? <span className="text-ink-muted">none</span>}</Td>
            <Td>
              <Counts counts={r.counts} />
            </Td>
            <Td>{r.requested_by ? <Mono>{r.requested_by}</Mono> : <span className="text-ink-muted">none</span>}</Td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}
