import { Info } from "lucide-react";
import type { SettingsOut } from "../../api/types";
import { Def, DefList } from "../../components/DefList";
import { EmptyState } from "../../components/EmptyState";
import { ErrorState } from "../../components/ErrorState";
import { Loading } from "../../components/Loading";
import { Mono } from "../../components/Mono";
import { Page } from "../../components/Page";
import { Panel } from "../../components/Panel";
import { DataTable, Td, Th } from "../../components/Table";
import { Time } from "../../components/Time";
import { ICON_SIZE } from "../../lib/icons";
import { FREQUENCIES } from "../operations/scheduleForm";
import { useSettings } from "./useSettings";

const yesNo = (b: boolean) => (b ? "Yes" : "No");
const frequency = (days: number) => FREQUENCIES.find((f) => f.value === days)?.label ?? `Every ${days} days`;

function scheduleSummary(s: SettingsOut["schedule"]): string {
  const source = s.source === "database" ? "saved in the database" : "server default from the environment";
  return `${s.enabled ? `${frequency(s.every_days)} at ${s.time} (${s.tz})` : "Switched off"}, ${source}`;
}

function Users({ users }: { users: SettingsOut["users"] }) {
  if (users.length === 0) return <EmptyState title="No users yet." hint="Add one from the command line: python -m app.cli users add <username>." />;
  return (
    <DataTable caption="Admin users">
      <thead>
        <tr>
          <Th>Username</Th>
          <Th>Active</Th>
          <Th>Last login</Th>
        </tr>
      </thead>
      <tbody>
        {users.map((u) => (
          <tr key={u.username}>
            <Td>
              <Mono>{u.username}</Mono>
            </Td>
            <Td>{yesNo(u.is_active)}</Td>
            <Td>{u.last_login_at ? <Time at={u.last_login_at} /> : "Never"}</Td>
          </tr>
        ))}
      </tbody>
    </DataTable>
  );
}

function Sections({ s }: { s: SettingsOut }) {
  return (
    <>
      <Panel title="Server">
        <DefList>
          <Def term="Environment">
            <Mono>{s.environment}</Mono>
          </Def>
          <Def term="StockTrim writes">
            {s.stocktrim_writes_enabled ? "Enabled: runs send changes to StockTrim." : "Off: runs plan the changes but send nothing."} <span className="text-ink-muted">Set on the server (STOCKTRIM_WRITES_ENABLED).</span>
          </Def>
          <Def term="Automatic runs">
            {s.scheduler_enabled ? "Allowed by the server." : "Switched off on the server."} <span className="text-ink-muted">Set on the server (SCHEDULER_ENABLED).</span>
          </Def>
          <Def term="Production runs allowed">
            {yesNo(s.scheduler_allow_production)} <span className="text-ink-muted">Set on the server (SCHEDULER_ALLOW_PRODUCTION).</span>
          </Def>
          <Def term="Sync schedule">{scheduleSummary(s.schedule)}</Def>
          <Def term="Planned-call limit per sync">
            <Mono>{s.sync_max_planned_calls}</Mono>
          </Def>
        </DefList>
      </Panel>
      <Panel title="Data and alerts">
        <DefList>
          <Def term="StockTrim host">{s.stocktrim_host ? <Mono>{s.stocktrim_host}</Mono> : "Not set"}</Def>
          <Def term="StockTrim requests per second">
            <Mono>{s.stocktrim_max_rps}</Mono>
          </Def>
          <Def term="QuickBooks read jobs">{s.read_jobs.length > 0 ? <Mono>{s.read_jobs.join(", ")}</Mono> : "None"}</Def>
          <Def term="Sales history (years)">
            <Mono>{s.sales_history_years}</Mono>
          </Def>
          <Def term="Negative stock policy">
            <Mono>{s.negative_stock_policy}</Mono>
          </Def>
          <Def term="Alert webhook configured">{yesNo(s.alert_webhook_configured)}</Def>
          <Def term="History kept (days)">
            <Mono>{s.history_retention_days}</Mono>
          </Def>
        </DefList>
      </Panel>
      <Panel title="Users">
        <Users users={s.users} />
      </Panel>
      {s.version && (
        <p className="text-ink-muted">
          Version <Mono>{s.version}</Mono>
        </p>
      )}
    </>
  );
}

export default function SettingsPage() {
  const q = useSettings();
  return (
    <Page title="Settings" purpose="How the server is configured. Read only.">
      <p className="flex items-start gap-2 text-ink-muted">
        <Info size={ICON_SIZE} aria-hidden="true" className="mt-1 shrink-0" />
        These are set on the server and cannot be changed here. The sync schedule is edited under Operations.
      </p>
      {q.isPending && <Loading label="Loading the settings" />}
      {q.isError && !q.data && <ErrorState message="The settings could not be loaded." onRetry={() => void q.refetch()} />}
      {q.data && <Sections s={q.data} />}
    </Page>
  );
}
