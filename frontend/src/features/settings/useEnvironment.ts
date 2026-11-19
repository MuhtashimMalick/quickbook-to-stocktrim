import { useSettings } from "./useSettings";

export type Environment = "test" | "production" | "unknown";

/** Fails safe: "unknown" while loading or on error; anything that is not exactly "test" counts as production. */
export function useEnvironment(): Environment {
  const { data } = useSettings();
  if (!data) return "unknown";
  return data.environment === "test" ? "test" : "production";
}
