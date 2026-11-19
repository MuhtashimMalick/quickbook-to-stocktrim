import * as Radix from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import type { ReactNode } from "react";
import { ICON_SIZE } from "../lib/icons";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description: string;
  /** Element to focus after closing (Radix only restores focus to a Dialog.Trigger, and this dialog is opened from a plain button). */
  returnFocus?: () => HTMLElement | null;
  children: ReactNode;
}

/** A small centred confirm dialog (Radix: focus trap, Esc, focus returns to the trigger, labelled by its title). No motion: it appears at once. */
export function Dialog({ open, onOpenChange, title, description, returnFocus, children }: Props) {
  return (
    <Radix.Root open={open} onOpenChange={onOpenChange}>
      <Radix.Portal>
        <Radix.Overlay className="fixed inset-0 z-30 bg-ink/40" />
        <Radix.Content
          onCloseAutoFocus={(event) => {
            const target = returnFocus?.();
            if (target) {
              event.preventDefault();
              target.focus();
            }
          }}
          className="fixed left-1/2 top-1/2 z-40 max-h-screen w-full max-w-lg -translate-x-1/2 -translate-y-1/2 overflow-y-auto border border-rule bg-sheet shadow-overlay">
          <div className="flex items-start justify-between gap-3 border-b border-rule px-4 py-3">
            <div className="min-w-0">
              <Radix.Title className="font-display text-heading font-semibold">{title}</Radix.Title>
              <Radix.Description className="text-ink-muted">{description}</Radix.Description>
            </div>
            <Radix.Close aria-label="Close" className="inline-flex size-row shrink-0 cursor-pointer items-center justify-center rounded-md border border-rule bg-sheet hover:border-ink">
              <X size={ICON_SIZE} aria-hidden="true" />
            </Radix.Close>
          </div>
          <div className="space-y-4 p-4">{children}</div>
        </Radix.Content>
      </Radix.Portal>
    </Radix.Root>
  );
}
