import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { routeTree } from "./routeTree.gen";

const queryClient = new QueryClient();

const router = createRouter({
  routeTree,
  // Start fetching a route's chunk (and loaders) when the user hovers/focuses a link.
  defaultPreload: "intent",
  context: undefined,
});

declare module "@tanstack/react-router" {
  interface Register {
    router: typeof router;
  }
  /** Per-route layout hints the shell reads (`__root.tsx`). */
  interface StaticDataRouteOption {
    /** Let the page use the full window width — for side-by-side workspaces. */
    wide?: boolean;
  }
}

const rootElement = document.getElementById("root");
if (!rootElement) {
  throw new Error("Root element #root not found");
}

createRoot(rootElement).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
