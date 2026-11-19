import { ErrorState } from "../../components/ErrorState";
import { Loading } from "../../components/Loading";
import { Mono } from "../../components/Mono";
import { Page } from "../../components/Page";
import { Panel } from "../../components/Panel";
import { useSettings } from "../settings/useSettings";

export default function OverviewPage() {
  const { data, isPending, isError, refetch } = useSettings();
  return (
    <Page title="Overview" purpose="What the sync is doing right now, and whether anything needs attention.">
      <Panel title="Server mode">
        {isPending && <Loading label="Loading server mode" />}
        {isError && <ErrorState message="The server settings could not be loaded." onRetry={() => void refetch()} />}
        {data && (
          <>
            <dl className="grid grid-cols-[max-content_1fr] gap-x-8 gap-y-2">
              <dt className="text-ink-muted">Environment</dt>
              <dd><Mono>{data.environment}</Mono></dd>
              <dt className="text-ink-muted">Writes</dt>
              <dd>{data.stocktrim_writes_enabled ? "Enabled" : "Disabled"}</dd>
              <dt className="text-ink-muted">Scheduler</dt>
              <dd>{data.scheduler_enabled ? "Enabled" : "Disabled"}</dd>
            </dl>
            {!data.stocktrim_writes_enabled && (
              <p className="mt-4 text-ink-muted">Writes are switched off on the server, so a run plans the changes but sends nothing.</p>
            )}
          </>
        )}
      </Panel>
    </Page>
  );
}
