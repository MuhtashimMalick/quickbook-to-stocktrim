import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { ICON_SIZE } from "../lib/icons";
import { useDrawerMotion } from "../lib/motion";

interface Props {
  open: boolean;
  /** Called once the exit motion has finished (immediately with reduced motion). */
  onClose: () => void;
  title: string;
  description: string;
  /** Element to focus after closing (Radix otherwise restores the element focused before it opened). */
  returnFocus?: () => HTMLElement | null;
  children: ReactNode;
}

interface LayerProps extends Omit<Props, "open"> {
  closing: boolean;
}

function Layer({ closing, onClose, title, description, returnFocus, children }: LayerProps) {
  const panel = useRef<HTMLDivElement>(null);
  const overlay = useRef<HTMLDivElement>(null);
  useDrawerMotion(panel, overlay, closing, onClose);
  return (
    <>
      <Dialog.Overlay ref={overlay} className="fixed inset-0 z-30 bg-ink/40" />
      <Dialog.Content
        ref={panel}
        onCloseAutoFocus={(event) => {
          const target = returnFocus?.();
          if (target) {
            event.preventDefault();
            target.focus();
          }
        }}
        className="fixed inset-y-0 right-0 z-40 flex w-full max-w-drawer flex-col overflow-y-auto border-l border-rule bg-sheet shadow-overlay"
      >
        <div className="flex items-start justify-between gap-3 border-b border-rule px-4 py-3">
          <div className="min-w-0">
            <Dialog.Title className="font-display text-heading font-semibold">{title}</Dialog.Title>
            <Dialog.Description className="text-ink-muted">{description}</Dialog.Description>
          </div>
          <Dialog.Close
            aria-label="Close"
            className="inline-flex size-row shrink-0 cursor-pointer items-center justify-center rounded-md border border-rule bg-sheet hover:border-ink"
          >
            <X size={ICON_SIZE} aria-hidden="true" />
          </Dialog.Close>
        </div>
        <div className="flex-1 p-4">{children}</div>
      </Dialog.Content>
    </>
  );
}

/** A right-side dialog (Radix: focus trap, Esc, labelled by its title). It stays mounted while its exit motion runs. */
export function Drawer({ open, ...rest }: Props) {
  const [closing, setClosing] = useState(false);
  if (!open && closing) setClosing(false); // closed from outside (browser Back): the next opening starts fresh
  const finish = () => {
    setClosing(false);
    rest.onClose();
  };
  return (
    <Dialog.Root open={open} onOpenChange={(next) => !next && setClosing(true)}>
      <Dialog.Portal>
        <Layer {...rest} onClose={finish} closing={closing} />
      </Dialog.Portal>
    </Dialog.Root>
  );
}
