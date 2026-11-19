import { render } from "@testing-library/react";
import type { ReactNode } from "react";
import { MemoryRouter, useLocation } from "react-router";
import { Providers } from "../app/providers";
import { AppRoutes } from "../app/routes";

function Location() {
  const { pathname, search } = useLocation();
  return <output data-testid="location">{pathname + search}</output>;
}

/** The whole app at a given URL (in-memory router). */
export function renderApp(path = "/") {
  const Router = ({ children }: { children: ReactNode }) => <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>;
  return render(
    <Providers Router={Router}>
      <AppRoutes />
      <Location />
    </Providers>,
  );
}
