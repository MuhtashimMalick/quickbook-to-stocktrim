import { useId, type SelectHTMLAttributes } from "react";
import { describe, FieldMessages } from "./FieldMessages";

interface Props extends SelectHTMLAttributes<HTMLSelectElement> {
  label: string;
  hint?: string;
  error?: string;
}

/** Visible label + native select (keyboard and screen-reader friendly by default). `className` sizes the wrapper. */
export function Select({ label, id, hint, error, className = "", children, ...rest }: Props) {
  const auto = useId();
  const selectId = id ?? auto;
  return (
    <div className={className}>
      <label htmlFor={selectId} className="mb-1 block font-medium">
        {label}
      </label>
      <select id={selectId} className={`h-row w-full rounded-md border bg-paper px-2 text-ink ${error ? "border-failed" : "border-rule"}`} {...describe(selectId, hint, error)} {...rest}>
        {children}
      </select>
      <FieldMessages id={selectId} hint={hint} error={error} />
    </div>
  );
}
