import { ICON_SIZE } from "../lib/icons";
import { Inbox } from "lucide-react";

export function EmptyState({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="flex items-start gap-3 py-2">
      <Inbox size={ICON_SIZE} aria-hidden="true" className="mt-1 shrink-0 text-ink-muted" />
      <div>
        <p className="font-medium">{title}</p>
        {hint && <p className="text-ink-muted">{hint}</p>}
      </div>
    </div>
  );
}
