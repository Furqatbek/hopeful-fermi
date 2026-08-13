import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { App } from "./app/App";
import "./styles.css";

const queries = new QueryClient({
  defaultOptions: {
    queries: {
      // Same reasoning as the console: a 4xx is an answer, not an outage.
      // Retrying a permission refusal spends the request budget in
      // `app/api/limits.py` and turns a clear refusal into a 429.
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
