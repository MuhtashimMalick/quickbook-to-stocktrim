/** Helper text and an inline error under a form control. Pair with `describe()` on the control. */
export function FieldMessages({ id, hint, error }: { id: string; hint?: string; error?: string }) {
  return (
    <>
      {hint && (
        <p id={`${id}-hint`} className="mt-1 text-table text-ink-muted">
          {hint}
        </p>
      )}
      {error && (
        <p id={`${id}-error`} role="alert" className="mt-1 text-table font-medium text-failed">
          {error}
        </p>
      )}
    </>
  );
}

/** The aria-describedby value and aria-invalid flag for a control with these messages. */
export function describe(id: string, hint?: string, error?: string) {
  const ids = [hint && `${id}-hint`, error && `${id}-error`].filter(Boolean).join(" ");
  return { "aria-describedby": ids || undefined, "aria-invalid": error ? true : undefined } as const;
}
