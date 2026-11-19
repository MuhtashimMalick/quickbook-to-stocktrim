import { ICON_SIZE } from "../lib/icons";
import { ChevronDown, LogOut, User } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useAuth } from "../features/auth/AuthProvider";

export function UserMenu() {
  const { state, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (!root.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
        toggle.current?.focus();
      }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  if (state.status !== "authenticated") return null;
  return (
    <div ref={root} className="relative">
      <button
        ref={toggle}
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex h-row cursor-pointer items-center gap-2 rounded-md px-2 hover:bg-paper"
      >
        <User size={ICON_SIZE} aria-hidden="true" />
        <span>{state.username}</span>
        <ChevronDown size={ICON_SIZE} aria-hidden="true" />
      </button>
      {open && (
        <div className="absolute right-0 z-10 mt-1 min-w-40 rounded-md border border-rule bg-sheet py-1 shadow-overlay">
          <button
            type="button"
            onClick={() => void logout()}
            className="flex h-row w-full cursor-pointer items-center gap-2 px-3 text-left hover:bg-paper"
          >
            <LogOut size={ICON_SIZE} aria-hidden="true" />
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
