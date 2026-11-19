import { EmptyState } from "../../components/EmptyState";
import { Mono } from "../../components/Mono";
import { Page } from "../../components/Page";
import { Panel } from "../../components/Panel";
import { useSettings } from "./useSettings";

export default function SettingsPage() {
  const { data } = useSettings();
  return (
    <Page title="Settings" purpose="How the server is configured, and the sync schedule.">
      <Panel>
        <EmptyState title="Nothing to change here yet." hint="Server settings and the schedule arrive in a later stage." />
      </Panel>
      {data?.version && (
        <p className="text-ink-muted">
          Version <Mono>{data.version}</Mono>
        </p>
      )}
    </Page>
  );
}
