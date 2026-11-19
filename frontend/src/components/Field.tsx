import { useId, type InputHTMLAttributes } from "react";
import { describe, FieldMessages } from "./FieldMessages";

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
  hint?: string;
  error?: string;
}

/** Visible label + input; never a placeholder-only label. Optional helper text and inline error. */
export function Field({ label, id, hint, error, className = "", ...rest }: Props) {
  const auto = useId();
  const inputId = id ?? auto;
  return (
    <div>
      <label htmlFor={inputId} className="mb-1 block font-medium">
        {label}
      </label>
      <input
        id={inputId}
        className={`h-row w-full rounded-md border bg-paper px-3 text-ink ${error ? "border-failed" : "border-rule"} ${className}`}
        {...describe(inputId, hint, error)}
        {...rest}
      />
      <FieldMessages id={inputId} hint={hint} error={error} />
    </div>
  );
}
