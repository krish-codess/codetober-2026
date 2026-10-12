import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";

// Cache policy, in one place: data is fresh for 10 s, kept 5 min after its last reader, refetched
// only where a query asks for an interval. Client errors are not retried; they will not get better.
const client = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      retry: (failures, error) => failures < 2 && !((error as { status?: number }).status! >= 400 && (error as { status?: number }).status! < 500),
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={client}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
