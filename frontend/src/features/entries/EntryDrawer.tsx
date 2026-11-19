import { ChevronLeft, ChevronRight } from "lucide-react";
import type { ReactNode } from "react";
import type { EntryOut, ReasonOut } from "../../api/types";
import { Button } from "../../components/Button";
import { CopyButton } from "../../components/CopyButton";
import { Drawer } from "../../components/Drawer";
import { Loading } from "../../components/Loading";
import { Mono } from "../../components/Mono";
import { StatusStamp } from "../../components/StatusStamp";
import { TextLink } from "../../components/TextLink";
import { Time } from "../../components/Time";
import { ICON_SIZE } from "../../lib/icons";
import { reasonOf } from "./EntriesTable";

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-ink-muted">{label}</dt>
      <dd className="flex min-w-0 flex-wrap items-center gap-2">{children}</dd>
    </>
  );
}

function Detail({ entry, catalog }: { entry: EntryOut; catalog: Record<string, ReasonOut> | undefined }) {
  const reason = reasonOf(entry, catalog);
  return (
    <div className="space-y-5">
      <StatusStamp status={entry.outcome} />
      <section aria-label="What happened" className="space-y-1">
        <h3 className="text-body">What happened</h3>
        <p>{reason.explanation}</p>
      </section>
      <section aria-label="What to do next" className="space-y-1">
        <h3 className="text-body">What to do next</h3>
        <p>{reason.next_step}</p>
      </section>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-2 border-t border-rule pt-4">
        <Row label="Identifier">
          <Mono className="break-all">{entry.key}</Mono>
          <CopyButton value={entry.key} label="Copy identifier" />
        </Row>
        {entry.ref && (
          <Row label="QuickBooks ID">
            <Mono className="break-all">{entry.ref}</Mono>
            <CopyButton value={entry.ref} label="Copy QuickBooks ID" />
          </Row>
        )}
        {entry.ref && <Row label="QuickBooks name">{entry.qb_label ?? <span className="text-ink-muted">Not in the current QuickBooks snapshot</span>}</Row>}
        <Row label="Entity">
          {entry.entity}
          {entry.action && <span className="text-ink-muted">{entry.action}</span>}
        </Row>
        {entry.code && (
          <Row label="Reason code">
            <Mono>{entry.code}</Mono>
          </Row>
        )}
        {entry.http_status !== null && (
          <Row label="HTTP status">
            <Mono>{entry.http_status}</Mono>
          </Row>
        )}
        {entry.fields.length > 0 && (
          <Row label="Rejected fields">
            <Mono>{entry.fields.join(", ")}</Mono>
          </Row>
        )}
        {entry.attempts !== null && (
          <Row label="Attempts">
            <Mono>{entry.attempts}</Mono>
          </Row>
        )}
        <Row label="First recorded">
          <Time at={entry.created_at} />
        </Row>
        <Row label="Last attempt">
          <Time at={entry.updated_at} />
        </Row>
        <Row label="Run">
          <TextLink to={`/runs/push/${entry.run_id}`}>Open this run</TextLink>
        </Row>
      </dl>
    </div>
  );
}

interface Props {
  open: boolean;
  entry: EntryOut | undefined;
  loading: boolean;
  catalog: Record<string, ReasonOut> | undefined;
  /** Position inside the loaded page, for previous / next. */
  position: { index: number; count: number } | null;
  onStep: (delta: -1 | 1) => void;
  onClose: () => void;
  returnFocus: () => HTMLElement | null;
}

/** Right-side detail of one entry, with previous / next through the loaded page. */
export function EntryDrawer({ open, entry, loading, catalog, position, onStep, onClose, returnFocus }: Props) {
  const title = entry ? reasonOf(entry, catalog).title : loading ? "Loading the entry" : "Entry not found";
  const description = entry ? `${entry.entity}: ${entry.key}` : "A record of the selected run";
  return (
    <Drawer open={open} onClose={onClose} title={title} description={description} returnFocus={returnFocus}>
      {entry ? (
        <>
          <Detail entry={entry} catalog={catalog} />
          {position && (
            <nav aria-label="Entries on this page" className="mt-6 flex items-center justify-between gap-3 border-t border-rule pt-4">
              <Button variant="secondary" disabled={position.index === 0} onClick={() => onStep(-1)}>
                <ChevronLeft size={ICON_SIZE} aria-hidden="true" />
                Previous entry
              </Button>
              <span className="text-ink-muted">
                <Mono>{position.index + 1}</Mono> of <Mono>{position.count}</Mono>
              </span>
              <Button variant="secondary" disabled={position.index === position.count - 1} onClick={() => onStep(1)}>
                Next entry
                <ChevronRight size={ICON_SIZE} aria-hidden="true" />
              </Button>
            </nav>
          )}
        </>
      ) : loading ? (
        <Loading label="Loading the entry" />
      ) : (
        <p>This entry is not in the run any more, or the link is not valid. Close this panel to go back to the list.</p>
      )}
    </Drawer>
  );
}
