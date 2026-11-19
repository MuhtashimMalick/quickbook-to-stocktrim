import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ComponentType, type ReactNode } from "react";
import { BrowserRouter } from "react-router";
import { AuthProvider } from "../features/auth/AuthProvider";

export function Providers({ children, Router = BrowserRouter }: { children: ReactNode; Router?: ComponentType<{ children: ReactNode }> }) {
  const [client] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false } } }));
  return (
    <QueryClientProvider client={client}>
      <Router>
        <AuthProvider>{children}</AuthProvider>
      </Router>
    </QueryClientProvider>
  );
}
