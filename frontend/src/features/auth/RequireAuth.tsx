import { Navigate, Outlet, useLocation } from "react-router";
import { Loading } from "../../components/Loading";
import { useAuth } from "./AuthProvider";

export function RequireAuth() {
  const { state } = useAuth();
  const { pathname, search, hash } = useLocation();
  if (state.status === "loading") return <Loading label="Checking your session" />;
  if (state.status === "anonymous") {
    return <Navigate to={`/login?next=${encodeURIComponent(pathname + search + hash)}`} replace />;
  }
  return <Outlet />;
}
