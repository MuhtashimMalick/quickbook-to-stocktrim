import { Component, Suspense, type ReactNode } from "react";
import { ErrorState } from "./ErrorState";
import { Loading } from "./Loading";

interface State {
  failed: boolean;
}

/** Catches a failed lazy chunk (e.g. the app was updated while the tab was open) or a render error. */
class Boundary extends Component<{ children: ReactNode }, State> {
  state: State = { failed: false };
  static getDerivedStateFromError(): State {
    return { failed: true };
  }
  render() {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="mx-auto max-w-page px-6 py-8">
        <ErrorState title="Page could not be shown" message="The app was updated or a page failed to load." onRetry={() => window.location.reload()} retryLabel="Reload" />
      </div>
    );
  }
}

/** Error boundary + Suspense around anything lazy (routes, the shell's Outlet). */
export function RouteErrorBoundary({ children }: { children: ReactNode }) {
  return (
    <Boundary>
      <Suspense fallback={<Loading />}>{children}</Suspense>
    </Boundary>
  );
}
