import { useId } from "react";

interface Option<T extends string | number> {
  value: T;
  label: string;
}

interface Props<T extends string | number> {
  legend: string;
  options: readonly Option<T>[];
  value: T;
  onChange: (value: T) => void;
}

/** A segmented control built from native radios in a fieldset: arrow keys move, the legend names the group, and the checked one has a fill. */
export function RadioGroup<T extends string | number>({ legend, options, value, onChange }: Props<T>) {
  const name = useId();
  return (
    <fieldset>
      <legend className="mb-1 font-medium">{legend}</legend>
      <div className="inline-flex flex-wrap">
        {options.map((o) => (
          <label key={o.value} className="relative -ml-px first:ml-0">
            <input type="radio" name={name} value={o.value} checked={o.value === value} onChange={() => onChange(o.value)} className="peer sr-only" />
            <span className="inline-flex h-row cursor-pointer items-center border border-rule bg-paper px-4 hover:border-ink peer-checked:z-10 peer-checked:border-accent peer-checked:bg-accent peer-checked:text-accent-ink peer-focus-visible:z-10 peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-accent">
              {o.label}
            </span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}
