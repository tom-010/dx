import {
  MutationCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { createRouter, RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { getCountUnreadNotificationsQueryKey } from "@/api/notifications/notifications";
import "./index.css";
import { routeTree } from "./routeTree.gen";

// Any write can produce a notification (`apps/notifications/services.py` is called by the app
// that owns the data, not by a page), so the bell's count is refreshed after every successful
// mutation rather than by each feature page remembering to invalidate it. It is one number
// behind its own tiny endpoint; the cost of asking again is a round trip nobody waits for.
const mutationCache = new MutationCache({
  onSuccess: (): void => {
    void queryClient.invalidateQueries({
      queryKey: getCountUnreadNotificationsQueryKey(),
    });
  },
});

const queryClient = new QueryClient({ mutationCache });

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
