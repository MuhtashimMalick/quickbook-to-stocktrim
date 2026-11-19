import { useId, type InputHTMLAttributes } from "react";

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  label: string;
}

/** Visible label + input; never a placeholder-only label. */
export function Field({ label, id, className = "", ...rest }: Props) {
  const auto = useId();
  const inputId = id ?? auto;
  return (
    <div>
      <label htmlFor={inputId} className="mb-1 block font-medium">
        {label}
      </label>
      <input
        id={inputId}
        className={`h-row w-full rounded-md border border-rule bg-paper px-3 text-ink ${className}`}
        {...rest}
      />
    </div>
  );
}
