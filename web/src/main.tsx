import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import "./styles.css";

const queries = new QueryClient({
  defaultOptions: {
    queries: {
      // A 403 is an answer, not an outage. Retrying a permission refusal three
      // times spends the request budget in `app/api/limits.py` and turns a clear
      // "you may not do that" into a 429 that reads like a broken server.
      retry: (count, error) => {
        const status = (error as { status?: number } | null)?.status;
        if (status && status >= 400 && status < 500) return false;
        return count < 2;
      },
      staleTime: 30_000,
    },
  },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queries}>
      <App />
    </QueryClientProvider>
  </StrictMode>,
);
