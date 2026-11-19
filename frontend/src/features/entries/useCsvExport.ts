import { useState } from "react";
import { api } from "../../api/client";
import { filenameFrom, saveBlob } from "../../lib/download";
import type { EntryParams } from "./params";

export const CSV_CAP = 200_000;

/**
 * CSV download of the current filters. The endpoint needs the Bearer header, so a plain link would 401: the file is fetched through the
 * authenticated client (token refresh included) and handed to the browser as a Blob.
 */
export function useCsvExport(runId: string, f: Pick<EntryParams, "outcomes" | "entity" | "code" | "q">) {
  const [status, setStatus] = useState<"idle" | "busy" | "error">("idle");
  const view = JSON.stringify([runId, f.outcomes, f.entity, f.code, f.q]);
  const [cut, setCut] = useState<string | null>(null); // the view whose export was cut: the notice goes away when the view changes

  async function run() {
    setStatus("busy");
    setCut(null);
    try {
      const { data, response } = await api.GET("/api/v1/push/runs/{run_id}/entries.csv", {
        params: { path: { run_id: runId }, query: { outcome: [...f.outcomes], entity: f.entity || undefined, code: f.code || undefined, q: f.q || undefined } },
        parseAs: "blob",
      });
      if (!response.ok || data === undefined) throw new Error(`HTTP ${response.status}`);
      saveBlob(data, filenameFrom(response, `entries-${runId.slice(0, 8)}.csv`));
      setCut(response.headers.get("X-Truncated") === "true" ? view : null);
      setStatus("idle");
    } catch {
      setStatus("error");
    }
  }

  return { status, truncated: cut === view, run };
}
