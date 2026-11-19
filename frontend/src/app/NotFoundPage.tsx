import { Link } from "react-router";
import { EmptyState } from "../components/EmptyState";
import { Page } from "../components/Page";
import { Panel } from "../components/Panel";

export default function NotFoundPage() {
  return (
    <Page title="Page not found" purpose="That address does not match any page.">
      <Panel>
        <EmptyState title="There is nothing at this address." hint="Check the link, or go back to the overview." />
        <Link to="/" className="mt-3 inline-block font-medium text-accent underline">
          Go to Overview
        </Link>
      </Panel>
    </Page>
  );
}
