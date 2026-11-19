import { Hourglass, SearchCheck } from "lucide-react";
import { ApiError } from "../../api/client";
import { Button } from "../../components/Button";
import { ErrorState } from "../../components/ErrorState";
import { Notice } from "../../components/Notice";
import { Panel } from "../../components/Panel";
import { ICON_SIZE } from "../../lib/icons";
import { usePreflight } from "./api";
import { PreflightReportView } from "./PreflightReportView";

export function PreflightPanel() {
  const check = usePreflight();
  const busy = check.error instanceof ApiError && check.error.status === 409;
  return (
    <Panel title="Pre-flight check">
      <div className="space-y-4">
        <p>Reads the current QuickBooks snapshot and StockTrim and reports what would be sent. Nothing is written.</p>
        <Button onClick={() => check.mutate()} disabled={check.isPending}>
          <SearchCheck size={ICON_SIZE} aria-hidden="true" />
          {check.isPending ? "Checking…" : "Run pre-flight check"}
        </Button>
        <div role="status" aria-live="polite">
          {check.isPending && <p className="text-ink-muted">Checking… this reads StockTrim and can take up to a minute.</p>}
        </div>
        {busy && (
          <Notice tone="info" icon={Hourglass} title="Another pre-flight check is already running.">
            <p>Wait a moment for it to finish, then run this one again.</p>
          </Notice>
        )}
        {check.isError && !busy && <ErrorState title="The check could not run" message="The server could not complete it. Nothing was changed." onRetry={() => check.mutate()} />}
        {check.data && <PreflightReportView report={check.data} />}
      </div>
    </Panel>
  );
}
