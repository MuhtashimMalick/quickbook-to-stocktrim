import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, awaitRefresh, refreshSession } from "../../api/client";
import { getGeneration, onSessionLost, setToken } from "../../api/session";

export type AuthState =
  | { status: "loading" }
  | { status: "anonymous" }
  | { status: "authenticated"; username: string };

export type LoginResult =
  | { ok: true }
  | { ok: false; kind: "invalid" | "rate_limited" | "error"; retryAfter?: number };

interface AuthContextValue {
  state: AuthState;
  /** true once a signed-in session was lost without the user asking (shows "You are signed out") */
  expired: boolean;
  login: (username: string, password: string) => Promise<LoginResult>;
  logout: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

async function loadMe(): Promise<AuthState> {
  const { data } = await api.GET("/api/v1/auth/me");
  return data ? { status: "authenticated", username: data.username } : { status: "anonymous" };
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>({ status: "loading" });
  const [expired, setExpired] = useState(false);
  const queryClient = useQueryClient();

  // silent refresh on load
  useEffect(() => {
    let alive = true;
    const generation = getGeneration();
    void (async () => {
      const next: AuthState =
        (await refreshSession()) === "ok" ? await loadMe().catch((): AuthState => ({ status: "anonymous" })) : { status: "anonymous" };
      // never overwrite a state set meanwhile (e.g. the user already signed in)
      if (alive && generation === getGeneration()) setState((prev) => (prev.status === "loading" ? next : prev));
    })();
    return () => {
      alive = false;
    };
  }, []);

  useEffect(
    () =>
      onSessionLost(() => {
        queryClient.clear();
        setExpired(true);
        setState({ status: "anonymous" });
      }),
    [queryClient],
  );

  const login = useCallback(
    async (username: string, password: string): Promise<LoginResult> => {
      try {
        const { data, response } = await api.POST("/api/v1/auth/login", { body: { username, password } });
        if (data) {
          setToken(data.access_token);
          const next = await loadMe();
          if (next.status !== "authenticated") {
            setToken(null);
            return { ok: false, kind: "error" };
          }
          setExpired(false);
          setState(next);
          return { ok: true };
        }
        if (response.status === 401) return { ok: false, kind: "invalid" };
        if (response.status === 429) {
          const seconds = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
          return { ok: false, kind: "rate_limited", retryAfter: Number.isFinite(seconds) ? seconds : undefined };
        }
        return { ok: false, kind: "error" };
      } catch {
        setToken(null);
        return { ok: false, kind: "error" };
      }
    },
    [],
  );

  const logout = useCallback(async () => {
    try {
      await awaitRefresh(); // a refresh still in flight must not revive the session after we log out
      await api.POST("/api/v1/auth/logout");
    } finally {
      // user asked for it: clear locally even if the call failed; not an "expired" session
      setToken(null);
      queryClient.clear();
      setExpired(false);
      setState({ status: "anonymous" });
    }
  }, [queryClient]);

  const value = useMemo(() => ({ state, expired, login, logout }), [state, expired, login, logout]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth must be used inside AuthProvider");
  return ctx;
}

