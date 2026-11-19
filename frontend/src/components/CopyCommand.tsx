import { Copy } from "lucide-react";
import { useState } from "react";
import { ICON_SIZE } from "../lib/icons";
import { Button } from "./Button";

/** The exact CLI command for an action the web app does not offer, with a copy button. */
export function CopyCommand({ command, label }: { command: string; label: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
    } catch {
      setCopied(false); // clipboard blocked: the command stays selectable above
    }
  }
  return (
    <div className="space-y-2">
      <pre tabIndex={0} aria-label={label} className="overflow-x-auto rounded-sm border border-rule bg-paper p-3 font-mono text-table">
        <code>{command}</code>
      </pre>
      <div className="flex items-center gap-3">
        <Button variant="secondary" onClick={() => void copy()}>
          <Copy size={ICON_SIZE} aria-hidden="true" />
          Copy command
        </Button>
        <span role="status" className="text-table text-ink-muted">
          {copied ? "Copied" : ""}
        </span>
      </div>
    </div>
  );
}
