import { lazy } from "react";
import { Route, Routes } from "react-router";
import { RouteErrorBoundary } from "../components/RouteErrorBoundary";
import { LoginPage } from "../features/auth/LoginPage";
import { RequireAuth } from "../features/auth/RequireAuth";
import { AppShell } from "./AppShell";

const OverviewPage = lazy(() => import("../features/overview/OverviewPage"));
const RunsPage = lazy(() => import("../features/runs/RunsPage"));
const EntriesPage = lazy(() => import("../features/entries/EntriesPage"));
const OperationsPage = lazy(() => import("../features/operations/OperationsPage"));
const SettingsPage = lazy(() => import("../features/settings/SettingsPage"));
const NotFoundPage = lazy(() => import("./NotFoundPage"));

export function AppRoutes() {
  return (
    <RouteErrorBoundary>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth />}>
          <Route element={<AppShell />}>
            <Route index element={<OverviewPage />} />
            <Route path="runs" element={<RunsPage />} />
            <Route path="entries" element={<EntriesPage />} />
            <Route path="operations" element={<OperationsPage />} />
            <Route path="settings" element={<SettingsPage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Route>
      </Routes>
    </RouteErrorBoundary>
  );
}
