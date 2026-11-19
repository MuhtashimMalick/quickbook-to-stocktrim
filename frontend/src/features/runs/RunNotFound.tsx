import { ApiError } from "../../api/client";
import { EmptyState } from "../../components/EmptyState";
import { Page } from "../../components/Page";
import { Panel } from "../../components/Panel";
import { TextLink } from "../../components/TextLink";

/** The API answers 404 for an unknown id and 422 for something that is not an id at all: both are "not found" to a person. */
export const isNotFound = (error: unknown): boolean => error instanceof ApiError && (error.status === 404 || error.status === 422);

export function RunNotFound({ what }: { what: string }) {
  return (
    <Page title={`${what} not found`} purpose="That address does not match anything we have on record.">
      <Panel>
        <EmptyState title={`There is no ${what.toLowerCase()} with this id.`} hint="It may be older than the history retention period, so it was removed." />
        <p className="mt-3">
          <TextLink to="/runs">Back to Runs</TextLink>
        </p>
      </Panel>
    </Page>
  );
}
