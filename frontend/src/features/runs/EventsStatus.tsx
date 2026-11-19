import { ErrorState } from "../../components/ErrorState";
import { Loading } from "../../components/Loading";

interface Q {
  isError: boolean;
  isPending: boolean;
  fetchStatus: string;
  refetch: () => Promise<unknown>;
}

/** Loading and error line for the (incremental) event queries: the events already shown stay visible when a poll fails. */
export function EventsStatus({ queries }: { queries: Q[] }) {
  const failed = queries.find((q) => q.isError);
  const loading = queries.some((q) => q.isPending && q.fetchStatus !== "idle");
  return (
    <>
      {loading && <Loading label="Loading events" />}
      {failed && <ErrorState title="Events could not be loaded" message="The newest events may be missing. Nothing else on this page is affected." onRetry={() => void Promise.all(queries.map((q) => q.refetch()))} />}
    </>
  );
}
