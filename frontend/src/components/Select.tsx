import { useId, type SelectHTMLAttributes } from "react";

interface Props extends SelectHTMLAttributes<HTMLSelectElement> {
  label: string;
}

/** Visible label + native select (keyboard and screen-reader friendly by default). `className` sizes the wrapper. */
export function Select({ label, id, className = "", children, ...rest }: Props) {
  const auto = useId();
  const selectId = id ?? auto;
  return (
    <div className={className}>
      <label htmlFor={selectId} className="mb-1 block font-medium">
        {label}
      </label>
      <select id={selectId} className="h-row w-full rounded-md border border-rule bg-paper px-2 text-ink" {...rest}>
        {children}
      </select>
    </div>
  );
}
