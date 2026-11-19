import { useState } from "react";

/** Copy text to the clipboard; `copied` flips to true on success (a blocked clipboard simply stays false). */
export function useCopy(): { copied: boolean; copy: (text: string) => Promise<void> } {
  const [copied, setCopied] = useState(false);
  async function copy(text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }
  return { copied, copy };
}
