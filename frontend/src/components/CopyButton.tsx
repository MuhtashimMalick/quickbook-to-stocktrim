import { Check, Copy } from "lucide-react";
import { useCopy } from "../lib/clipboard";
import { ICON_SIZE } from "../lib/icons";

/** Small icon button that copies `value`; `label` names what is copied ("Copy key"). Announces "Copied" politely. */
export function CopyButton({ value, label }: { value: string; label: string }) {
  const { copied, copy } = useCopy();
  return (
    <>
      <button
        type="button"
        aria-label={label}
        title={label}
        onClick={() => void copy(value)}
        className="inline-flex size-row shrink-0 cursor-pointer items-center justify-center rounded-md border border-rule bg-sheet text-ink hover:border-ink"
      >
        {copied ? <Check size={ICON_SIZE} aria-hidden="true" /> : <Copy size={ICON_SIZE} aria-hidden="true" />}
      </button>
      <span role="status" className="sr-only">
        {copied ? "Copied" : ""}
      </span>
    </>
  );
}
