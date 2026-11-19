import type { ButtonHTMLAttributes, Ref } from "react";

const VARIANTS = {
  primary: "bg-accent text-accent-ink hover:bg-ink border-accent hover:border-ink",
  secondary: "bg-sheet text-ink border-rule hover:border-ink",
} as const;

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: keyof typeof VARIANTS;
  ref?: Ref<HTMLButtonElement>;
}

export function Button({ variant = "primary", className = "", type = "button", ...rest }: Props) {
  return (
    <button
      type={type}
      className={`inline-flex h-row cursor-pointer items-center justify-center gap-2 rounded-md border px-4 font-medium disabled:cursor-not-allowed disabled:opacity-60 ${VARIANTS[variant]} ${className}`}
      {...rest}
    />
  );
}
