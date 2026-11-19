import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";
import type { EntryOut } from "../../api/types";

export const OUTCOMES = ["failed", "skipped", "excluded"] as const;
export type Outcome = (typeof OUTCOMES)[number];
export const DEFAULT_OUTCOMES: readonly Outcome[] = ["failed", "skipped"];
export const PAGE_SIZE = 50;

/** All page state. It lives in the URL query, so every view can be shared and deep-linked. */
export interface EntryParams {
  run: string | null;
  outcomes: readonly Outcome[];
  entity: string;
  code: string;
  q: string;
  page: number; // 1-based
  entry: string | null;
}

const isOutcome = (v: string): v is Outcome => (OUTCOMES as readonly string[]).includes(v);
const sameOutcomes = (a: readonly Outcome[], b: readonly Outcome[]) => a.length === b.length && a.every((o) => b.includes(o));

export function parseParams(sp: URLSearchParams): EntryParams {
  const picked = OUTCOMES.filter((o) => sp.getAll("outcome").includes(o)).filter(isOutcome);
  const page = Number.parseInt(sp.get("page") ?? "", 10);
  return {
    run: sp.get("run") || null,
    outcomes: picked.length > 0 ? picked : DEFAULT_OUTCOMES,
    entity: sp.get("entity") ?? "",
    code: sp.get("code") ?? "",
    q: sp.get("q") ?? "",
    page: Number.isFinite(page) && page > 1 ? page : 1,
    entry: sp.get("entry") || null,
  };
}

/** Defaults are left out, so a plain /entries stays plain. */
export function serializeParams(p: EntryParams): URLSearchParams {
  const sp = new URLSearchParams();
  if (p.run) sp.set("run", p.run);
  if (!sameOutcomes(p.outcomes, DEFAULT_OUTCOMES)) p.outcomes.forEach((o) => sp.append("outcome", o));
  if (p.entity) sp.set("entity", p.entity);
  if (p.code) sp.set("code", p.code);
  if (p.q) sp.set("q", p.q);
  if (p.page > 1) sp.set("page", String(p.page));
  if (p.entry) sp.set("entry", p.entry);
  return sp;
}

export const hasNarrowing = (p: EntryParams): boolean => Boolean(p.entity || p.code || p.q);

/**
 * The API has no entry id. An entry is identified by what makes it unique within a run: its table (source), entity, key and reason code.
 * The key may contain the separator, which is fine: ids are only compared, never parsed apart from \`parseEntryId\`.
 */
export const entryId = (e: Pick<EntryOut, "source" | "entity" | "key" | "code">): string => [e.source, e.entity, e.key, e.code ?? ""].join("|");

/** The parts of an id that can narrow a lookup (source and entity come first, the code last, the key is whatever is between). */
export function parseEntryId(id: string): { entity: string; key: string; code: string } | null {
  const parts = id.split("|");
  if (parts.length < 4) return null;
  return { entity: parts[1] ?? "", key: parts.slice(2, -1).join("|"), code: parts.at(-1) ?? "" };
}

type Patch = Partial<EntryParams>;

export function useEntryParams() {
  const [sp, setSp] = useSearchParams();
  const params = useMemo(() => parseParams(sp), [sp]);
  /** Change the page, run, entry or any filter. \`replace\` keeps the back button from stepping through every keystroke or chip. */
  const update = useCallback((patch: Patch, replace = true) => setSp((prev) => serializeParams({ ...parseParams(prev), ...patch }), { replace }), [setSp]);
  /** A filter or run change starts again at page 1 with no entry open. */
  const setFilters = useCallback((patch: Patch, replace = true) => update({ ...patch, page: 1, entry: null }, replace), [update]);
  return { params, update, setFilters };
}
