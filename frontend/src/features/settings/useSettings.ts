import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "../../api/client";
import { keys } from "../../api/queryKeys";

export function useSettings() {
  return useQuery({
    queryKey: keys.settings,
    queryFn: () => unwrap(api.GET("/api/v1/settings")),
    staleTime: 60_000,
  });
}
