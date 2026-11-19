import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "../../api/client";

export function useSettings() {
  return useQuery({
    queryKey: ["settings"],
    queryFn: () => unwrap(api.GET("/api/v1/settings")),
    staleTime: 60_000,
  });
}
