import type { EntryOut, ReasonOut } from "../../api/types";
import { Mono } from "../../components/Mono";
import { StatusStamp } from "../../components/StatusStamp";
import { DataTable, RowTh, Td, Th } from "../../components/Table";
import { TextLink } from "../../components/TextLink";
import { Time } from "../../components/Time";
import { entryId } from "./params";

/** Catalog entry of the code if the catalog has it, else the explanation that came with the entry, else the bare code. */
export function reasonOf(e: EntryOut, catalog: Record<string, ReasonOut> | undefined): ReasonOut {
  const known = e.code ? catalog?.[e.code] : undefined;
  const reason = known ?? e.reason;
  return { ...reason, title: reason.title || e.code || "No reason recorded" };
}

interface Props {
  items: EntryOut[];
  catalog: Record<string, ReasonOut> | undefined;
  onOpen: (id: string) => void;
}

/** The entries of one page. The identifier is a real button that opens the detail drawer; the whole row is a larger click target. */
export function EntriesTable({ items, catalog, onOpen }: Props) {
  return (
    <DataTable caption="Entries of the selected run, most recently updated first">
      <thead>
        <tr>
          <Th>Outcome</Th>
          <Th>Entity</Th>
          <Th>Identifier</Th>
          <Th>Reason</Th>
          <Th align="right">Attempts</Th>
          <Th>Last attempt</Th>
          <Th>Run</Th>
        </tr>
      </thead>
      <tbody>
        {items.map((e) => {
          const id = entryId(e);
          return (
            <tr key={id} onClick={() => onOpen(id)} className="cursor-pointer hover:bg-paper">
              <Td>
                <StatusStamp status={e.outcome} />
              </Td>
              <Td>
                {e.entity}
                {e.action && <span className="text-ink-muted"> {e.action}</span>}
              </Td>
              <RowTh>
                <button
                  type="button"
                  data-entry-id={id}
                  aria-haspopup="dialog"
                  className="cursor-pointer break-all text-left font-mono font-medium tabular-nums text-accent underline underline-offset-2 hover:text-ink"
                >
                  {e.key}
                </button>
                {e.qb_label && <div className="font-normal text-ink-muted">{e.qb_label}</div>}
              </RowTh>
              <Td>
                <span title={e.code ?? undefined}>{reasonOf(e, catalog).title}</span>
              </Td>
              <Td align="right">{e.attempts === null ? <span className="text-ink-muted">-</span> : <Mono>{e.attempts}</Mono>}</Td>
              <Td>
                <Time at={e.updated_at} />
              </Td>
              <Td>
                <TextLink to={`/runs/push/${e.run_id}`} onClick={(ev) => ev.stopPropagation()}>
                  <Mono>{e.run_id.slice(0, 8)}</Mono>
                </TextLink>
              </Td>
            </tr>
          );
        })}
      </tbody>
    </DataTable>
  );
}

const SKELETON_ROWS = 8;

/** Ruled placeholder rows while the first page loads (static, no looping motion). */
export function TableSkeleton() {
  return (
    <div role="status">
      <span className="sr-only">Loading the entries…</span>
      <div aria-hidden="true">
        {Array.from({ length: SKELETON_ROWS }, (_, i) => (
          <div key={i} className="flex h-row items-center gap-4 border-b border-rule">
            <div className="h-3 w-20 bg-rule" />
            <div className="h-3 w-24 bg-rule" />
            <div className="h-3 w-48 bg-rule" />
            <div className="h-3 flex-1 bg-rule" />
          </div>
        ))}
      </div>
    </div>
  );
}
