import { Terminal } from "lucide-react";
import { CopyCommand } from "../../components/CopyCommand";
import { Panel } from "../../components/Panel";
import { ICON_SIZE } from "../../lib/icons";
import { cancelCommand, resumeCommand } from "../runs/cli";

const push = (body: string) => `echo '${body}' | docker compose exec -T app python -m app.cli POST /push --json`;

/** Commands from backend/README.md ("Push engine"). Placeholders in angle brackets come from a dry-run or the run itself. */
export const COMMANDS = [
  { label: "Plan a push (dry run, sends nothing)", command: push('{"steps": ["suppliers", "products", "bom", "customers"]}') },
  {
    label: "Run a real push",
    command: push('{"steps": ["suppliers", "products", "bom", "customers"], "dry_run": false, "preflight_fingerprint": "<from the dry-run>", "plan_fingerprint": "<from the dry-run>"}'),
  },
  { label: "Resume a push run", command: resumeCommand("<run_id>") },
  { label: "Cancel a push run", command: cancelCommand("<run_id>") },
] as const;

export function MoreCommands() {
  return (
    <Panel title="Need to do more?">
      <div className="space-y-3">
        <p>Some actions are deliberately not available in the browser. They run from the server&apos;s command line:</p>
        <ul className="list-disc space-y-1 pl-5">
          <li>Real pushes to StockTrim (they need the dry-run&apos;s fingerprints).</li>
          <li>Resuming or cancelling a push run.</li>
          <li>Acknowledging foreign StockTrim records (add the verdict&apos;s hash as acknowledge_foreign).</li>
        </ul>
        <details className="border border-rule bg-paper">
          <summary className="flex min-h-row cursor-pointer items-center gap-2 px-3 font-medium">
            <Terminal size={ICON_SIZE} aria-hidden="true" />
            Show the commands
          </summary>
          <div className="space-y-4 border-t border-rule p-3">
            {COMMANDS.map((c) => (
              <div key={c.label} className="space-y-1">
                <p className="font-medium">{c.label}</p>
                <CopyCommand command={c.command} label={c.label} />
              </div>
            ))}
            <p className="text-table text-ink-muted">
              Run them from the repository root. Details and every option are in <span className="font-mono">backend/README.md</span>, section &quot;Push engine&quot;.
            </p>
          </div>
        </details>
      </div>
    </Panel>
  );
}
