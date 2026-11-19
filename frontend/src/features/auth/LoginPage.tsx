import { useEffect, useRef, useState, type FormEvent } from "react";
import { Navigate, useSearchParams } from "react-router";
import { Loading } from "../../components/Loading";
import { Button } from "../../components/Button";
import { Field } from "../../components/Field";
import { safeNext } from "../../lib/safeNext";
import { useAuth, type LoginResult } from "./AuthProvider";

export const LOGIN_MESSAGES = {
  invalid: "Invalid credentials, or too many attempts: wait 15 minutes",
  error: "Could not reach the server. Check your connection and try again.",
  expired: "You are signed out. Sign in again.",
} as const;

function messageFor(result: Extract<LoginResult, { ok: false }>): string {
  if (result.kind === "rate_limited") {
    return result.retryAfter === undefined
      ? "Too many requests. Wait a moment and try again."
      : `Too many requests. Try again in ${result.retryAfter} seconds.`;
  }
  return LOGIN_MESSAGES[result.kind];
}

export function LoginPage() {
  const { state, expired, login } = useAuth();
  const [params] = useSearchParams();
  const next = safeNext(params.get("next"));
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const errorRef = useRef<HTMLParagraphElement>(null);

  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);

  if (state.status === "loading") return <Loading label="Checking your session" />;
  if (state.status === "authenticated") return <Navigate to={next} replace />;

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setSubmitting(true);
    setError(null);
    const result = await login(String(form.get("username") ?? ""), String(form.get("password") ?? ""));
    setSubmitting(false);
    if (!result.ok) setError(messageFor(result));
  }

  return (
    <main id="main" className="mx-auto grid min-h-screen max-w-login items-center gap-10 px-6 md:grid-cols-[1fr_380px]">
      <div className="hidden md:block">
        <p className="font-mono text-table uppercase tracking-widest text-ink-muted">Dispatch ledger</p>
        <p className="mt-3 font-display text-title font-semibold">QuickBook</p>
        <p className="mt-3 max-w-md text-ink-muted">
          Review what was read from QuickBooks, what was planned for StockTrim and what was sent.
        </p>
      </div>
      <form onSubmit={onSubmit} className="border border-rule bg-sheet p-6" noValidate>
        <p className="font-mono text-table uppercase tracking-widest text-ink-muted md:hidden">QuickBook</p>
        <h1 className="text-heading">Sign in</h1>
        {expired && !error && <p className="mt-3 text-ink-muted">{LOGIN_MESSAGES.expired}</p>}
        {error && (
          <p ref={errorRef} tabIndex={-1} role="alert" className="mt-3 border-l-2 border-failed bg-failed-tint px-3 py-2 text-failed">
            {error}
          </p>
        )}
        <div className="mt-5 space-y-4">
          <Field label="Username" name="username" autoComplete="username" required autoFocus={!error} />
          <Field label="Password" name="password" type="password" autoComplete="current-password" required />
        </div>
        <Button type="submit" className="mt-6 w-full" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </main>
  );
}
