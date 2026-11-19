import { NavLink, Outlet, useLocation } from "react-router";
import { LiveRegion } from "../components/LiveRegion";
import { RouteErrorBoundary } from "../components/RouteErrorBoundary";
import { SessionEndNotice } from "../features/auth/SessionEndNotice";
import { useEnvironment } from "../features/settings/useEnvironment";
import { EnvTag, ProductionBanner, WritesChip } from "./EnvironmentBar";
import { UserMenu } from "./UserMenu";

export const NAV = [
  { to: "/", label: "Overview", end: true },
  { to: "/runs", label: "Runs", end: false },
  { to: "/entries", label: "Entries", end: false },
  { to: "/operations", label: "Operations", end: false },
  { to: "/settings", label: "Settings", end: false },
] as const;

const linkClass = ({ isActive }: { isActive: boolean }) =>
  `inline-flex h-row items-center border-b-2 px-1 font-medium ${isActive ? "border-accent text-accent" : "border-transparent text-ink-muted hover:text-ink"}`;

export function AppShell() {
  const env = useEnvironment();
  const { pathname } = useLocation();
  const current = NAV.find((n) => (n.end ? pathname === n.to : pathname.startsWith(n.to)));

  return (
    <div data-env={env} className="min-h-screen bg-paper">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-20 focus:rounded-md focus:bg-sheet focus:px-3 focus:py-2">
        Skip to main content
      </a>
      <ProductionBanner />
      <header className={`border-b border-rule bg-sheet ${env === "test" ? "border-t-2 border-t-accent" : ""}`}>
        <div className="mx-auto flex max-w-page flex-wrap items-center gap-x-6 gap-y-1 px-6 py-2">
          <span className="font-display text-heading font-semibold">QuickBook</span>
          <EnvTag />
          <nav aria-label="Main" className="order-last flex w-full gap-5 md:order-none md:w-auto">
            {NAV.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end} className={linkClass}>
                {n.label}
              </NavLink>
            ))}
          </nav>
          <div className="ml-auto flex items-center gap-3">
            <WritesChip />
            <UserMenu />
          </div>
        </div>
      </header>
      <SessionEndNotice />
      <main id="main" tabIndex={-1} className="mx-auto max-w-page px-6 py-8">
        <RouteErrorBoundary key={pathname}>
          <Outlet />
        </RouteErrorBoundary>
      </main>
      <LiveRegion message={current ? `${current.label} page` : ""} />
    </div>
  );
}
