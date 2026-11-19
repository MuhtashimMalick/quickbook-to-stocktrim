import { CircleAlert, Info, TriangleAlert, type LucideIcon } from "lucide-react";
import { useState } from "react";
import type { EventOut } from "../../api/types";
import { Button } from "../../components/Button";
import { EmptyState } from "../../components/EmptyState";
import { Mono } from "../../components/Mono";
import { DataTable, Td, Th } from "../../components/Table";
import { Time } from "../../components/Time";
import { ICON_SIZE } from "../../lib/icons";
import { Counts } from "./Counts";

const LEVELS = ["all", "info", "warning", "error"] as const;
type Filter = (typeof LEVELS)[number];

const LEVEL: Record<string, { icon: LucideIcon; classes: string }> = {
  info: { icon: Info, classes: "text-running" },
  warning: { icon: TriangleAlert, classes: "text-aborted" },
  error: { icon: CircleAlert, classes: "text-failed" },
};

/** Events, newest first, with a level filter. An unknown level is shown as its own word and counts under "all" only. */
export function EventLog({ events }: { events: EventOut[] }) {
  const [filter, setFilter] = useState<Filter>("all");
  const shown = events.filter((e) => filter === "all" || e.level === filter).sort((a, b) => b.id - a.id);
  return (
    <div className="space-y-3">
      <div role="group" aria-label="Filter events by level" className="flex flex-wrap gap-2">
        {LEVELS.map((l) => (
          <Button key={l} variant={filter === l ? "primary" : "secondary"} aria-pressed={filter === l} onClick={() => setFilter(l)}>
            {l === "all" ? "All" : l.charAt(0).toUpperCase() + l.slice(1)}
          </Button>
        ))}
      </div>
      {shown.length === 0 ? (
        <EmptyState title={events.length === 0 ? "No events were recorded." : `No ${filter} events.`} hint={events.length === 0 ? "Events are kept for the history retention period, then removed." : undefined} />
      ) : (
        <DataTable caption="Run events, newest first">
          <thead>
            <tr>
              <Th>Time</Th>
              <Th>Level</Th>
              <Th>Event</Th>
              <Th>Entity</Th>
              <Th>Counts</Th>
              <Th>By</Th>
            </tr>
          </thead>
          <tbody>
            {shown.map((e) => {
              const lv = LEVEL[e.level];
              const Icon = lv?.icon ?? Info;
              return (
                <tr key={e.id}>
                  <Td>
                    <Time at={e.at} format="clock" />
                  </Td>
                  <Td>
                    <span className={`inline-flex items-center gap-1 font-semibold uppercase ${lv?.classes ?? "text-ink-muted"}`}>
                      <Icon size={ICON_SIZE} aria-hidden="true" />
                      {e.level}
                    </span>
                  </Td>
                  <Td>
                    <div>{e.title}</div>
                    <Mono className="text-ink-muted">{e.code}</Mono>
                  </Td>
                  <Td>{e.entity ? <Mono>{e.entity}</Mono> : null}</Td>
                  <Td>{e.counts ? <Counts counts={e.counts} /> : null}</Td>
                  <Td>{e.actor ? <Mono>{e.actor}</Mono> : null}</Td>
                </tr>
              );
            })}
          </tbody>
        </DataTable>
      )}
    </div>
  );
}
