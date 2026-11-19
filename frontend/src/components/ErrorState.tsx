import { ICON_SIZE } from "../lib/icons";
import { TriangleAlert } from "lucide-react";
import { Button } from "./Button";

export function ErrorState({ title = "Something went wrong", message, onRetry, retryLabel = "Try again" }: { title?: string; message: string; onRetry?: () => void; retryLabel?: string }) {
  return (
    <div role="alert" className="flex items-start gap-3 border-l-2 border-failed bg-failed-tint px-4 py-3 text-failed">
      <TriangleAlert size={ICON_SIZE} aria-hidden="true" className="mt-1 shrink-0" />
      <div>
        <p className="font-semibold">{title}</p>
        <p>{message}</p>
        {onRetry && (
          <Button variant="secondary" className="mt-3 text-ink" onClick={onRetry}>
            {retryLabel}
          </Button>
        )}
      </div>
    </div>
  );
}
