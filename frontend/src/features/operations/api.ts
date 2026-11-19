import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, unwrap } from "../../api/client";
import { keys } from "../../api/queryKeys";
import type { ScheduleIn } from "../../api/types";

export function useSchedule() {
  return useQuery({
    queryKey: keys.schedule,
    queryFn: () => unwrap(api.GET("/api/v1/schedule")),
  });
}

/** PUT /schedule. The answer is the new schedule (with its next runs), so the cache is set from it; overview and settings show the schedule too. */
export function useSaveSchedule() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (body: ScheduleIn) => unwrap(api.PUT("/api/v1/schedule", { body })),
    onSuccess: (saved) => {
      qc.setQueryData(keys.schedule, saved);
      void qc.invalidateQueries({ queryKey: keys.settings });
      void qc.invalidateQueries({ queryKey: keys.overview });
    },
  });
}

/** POST /scheduler/run-now: 202 with the new cycle's id. */
export function useRunNow() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/scheduler/run-now")),
    onSettled: () => void qc.invalidateQueries({ queryKey: keys.overview }),
  });
}

/** POST /preflight with the default options (read-only against StockTrim; can take a while). */
export function usePreflight() {
  return useMutation({ mutationFn: () => unwrap(api.POST("/api/v1/preflight")) });
}
