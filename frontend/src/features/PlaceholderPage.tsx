import { EmptyState } from "../components/EmptyState";
import { Page } from "../components/Page";
import { Panel } from "../components/Panel";

/** F1 placeholder: title, purpose and the shared empty state. Real content arrives in later stages. */
export function PlaceholderPage({ title, purpose, empty, hint }: { title: string; purpose: string; empty: string; hint: string }) {
  return (
    <Page title={title} purpose={purpose}>
      <Panel>
        <EmptyState title={empty} hint={hint} />
      </Panel>
    </Page>
  );
}
