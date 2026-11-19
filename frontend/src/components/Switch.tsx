import { useId, type InputHTMLAttributes } from "react";

interface Props extends Omit<InputHTMLAttributes<HTMLInputElement>, "type" | "role"> {
  label: string;
  hint?: string;
}

/** A native checkbox exposed as a switch: space toggles it, its label is visible, and on/off shows as a word as well as position. */
export function Switch({ label, hint, id, checked, className = "", ...rest }: Props) {
  const auto = useId();
  const inputId = id ?? auto;
  return (
    <div className={className}>
      <label htmlFor={inputId} className="inline-flex cursor-pointer items-center gap-3 font-medium">
        <input id={inputId} type="checkbox" role="switch" checked={checked} aria-describedby={hint ? `${inputId}-hint` : undefined} className="peer sr-only" {...rest} />
        <span
          aria-hidden="true"
          className="relative h-5 w-10 shrink-0 rounded-sm border border-ink-muted bg-paper peer-checked:border-accent peer-checked:bg-accent peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-accent peer-disabled:opacity-60"
        >
          <span className={`absolute top-0.5 size-3.5 rounded-sm ${checked ? "left-5.5 bg-accent-ink" : "left-0.5 bg-ink-muted"}`} />
        </span>
        {label}
        <span aria-hidden="true" className="font-mono text-table text-ink-muted">
          {checked ? "ON" : "OFF"}
        </span>
      </label>
      {hint && (
        <p id={`${inputId}-hint`} className="mt-1 text-table text-ink-muted">
          {hint}
        </p>
      )}
    </div>
  );
}
