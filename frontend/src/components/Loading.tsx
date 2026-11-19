/** Static ruled placeholder lines (no looping animation) with a polite status for assistive tech. */
export function Loading({ label = "Loading" }: { label?: string }) {
  return (
    <div role="status" className="p-4">
      <span className="sr-only">{label}…</span>
      <div aria-hidden="true" className="space-y-3">
        <div className="h-3 w-1/3 bg-rule" />
        <div className="h-3 w-full bg-rule" />
        <div className="h-3 w-2/3 bg-rule" />
      </div>
    </div>
  );
}
