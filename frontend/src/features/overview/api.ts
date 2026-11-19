import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "../../api/client";
import { keys } from "../../api/queryKeys";
import type { Overview } from "../../api/types";
import { usePollInterval } from "../../lib/polling";

/** A cycle or a push run is running: poll every 2 s instead of every 30 s. */
export const isLive = (o: Overview | undefined): boolean => Boolean(o?.running_cycle ?? o?.running_push);

/** One call drives the whole overview. */
export function useOverview() {
  const refetchInterval = usePollInterval<Overview>(isLive);
  return useQuery({
    queryKey: keys.overview,
    queryFn: () => unwrap(api.GET("/api/v1/overview")),
    refetchInterval,
  });
}
