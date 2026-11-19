import { useRef } from "react";
import { useProgressTween } from "../lib/motion";

/** A thin bar; the fill tweens (transform only) unless the user prefers reduced motion. */
export function ProgressBar({ value, max, label }: { value: number; max: number; label: string }) {
  const fill = useRef<HTMLDivElement>(null);
  useProgressTween(fill, max > 0 ? Math.min(1, value / max) : 0);
  return (
    <div role="progressbar" aria-label={label} aria-valuemin={0} aria-valuemax={max} aria-valuenow={value} className="h-2 overflow-hidden rounded-sm bg-rule">
      <div ref={fill} className="h-full origin-left bg-accent" />
    </div>
  );
}
