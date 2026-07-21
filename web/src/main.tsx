import { QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";

import { queryClient } from "./api/query-client";
import { AppProviders } from "./app/providers";
import App from "./App";
import "./styles/global.css";
import "./styles/layout.css";
import "./styles/shell.css";
import "./styles/utilities.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <AppProviders>
          <App />
        </AppProviders>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
