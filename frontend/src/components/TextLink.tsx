import { Link, type LinkProps } from "react-router";

export function TextLink({ className = "", ...rest }: LinkProps) {
  return <Link className={`font-medium text-accent underline underline-offset-2 hover:text-ink ${className}`} {...rest} />;
}
