import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";
import { ICON_SIZE } from "../lib/icons";

const TONES = {
  info: "border-running bg-running-tint",
  warn: "border-aborted bg-aborted-tint",
  neutral: "border-dry-run bg-dry-run-tint",
} as const;

/** A calm call-out: icon + a short title + text, on a tinted ruled edge. Failures use ErrorState instead. */
export function Notice({ tone, icon: Icon, title, children }: { tone: keyof typeof TONES; icon: LucideIcon; title: string; children?: ReactNode }) {
  return (
    <div className={`flex items-start gap-3 border-l-2 px-4 py-3 ${TONES[tone]}`}>
      <Icon size={ICON_SIZE} aria-hidden="true" className="mt-1 shrink-0" />
      <div className="min-w-0 space-y-2">
        <p className="font-semibold">{title}</p>
        {children}
      </div>
    </div>
  );
}
