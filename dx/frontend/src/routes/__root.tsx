import { useQueryClient } from "@tanstack/react-query";
import {
  createRootRoute,
  Link,
  Outlet,
  type ParsedLocation,
  redirect,
  useLocation,
  useMatches,
  useNavigate,
} from "@tanstack/react-router";
import {
  FileText,
  House,
  Images,
  ListChecks,
  NotebookPen,
  Table2,
} from "lucide-react";
import { type JSX, useEffect } from "react";
import { logout as logoutRequest, useGetCurrentUser } from "@/api/auth/auth";
import { Button } from "@/components/ui/button";
import {
  getAccessToken,
  getRefreshToken,
  setTokens,
  useAccessToken,
} from "@/lib/auth";
import { cn } from "@/lib/utils";

const LOGIN_PATH = "/login";

export const Route = createRootRoute({
  // Every page except /login needs a token; the API rejects calls without one anyway.
  beforeLoad: ({ location }: { location: ParsedLocation }): void => {
    if (getAccessToken() === null && location.pathname !== LOGIN_PATH) {
      throw redirect({ to: LOGIN_PATH });
    }
  },
  component: RootLayout,
  notFoundComponent: NotFound,
});

// The same six destinations twice over: a tab bar under the thumb on phones, a row of
// links in the header from md up. The icon is only ever shown in the tab bar.
const navItems = [
  { to: "/", label: "Home", exact: true, icon: House },
  { to: "/datasets", label: "Datasets", exact: false, icon: Table2 },
  { to: "/documents", label: "Documents", exact: false, icon: FileText },
  { to: "/gallery", label: "Gallery", exact: false, icon: Images },
  { to: "/notes", label: "Notes", exact: false, icon: NotebookPen },
  { to: "/tasks", label: "Tasks", exact: false, icon: ListChecks },
] as const;

function RootLayout(): JSX.Element {
  const token = useAccessToken();
  const location = useLocation();
  const navigate = useNavigate();
  // Pages are centred at a readable width; a route with `staticData: { wide: true }` (the
  // document workspace) asks for the whole window instead, because it shows two panes.
  const matches = useMatches();
  const width = matches.some((match) => match.staticData.wide === true)
    ? "max-w-[120rem]"
    : "max-w-5xl";

  // The session can end while a page is open (refresh rejected, logged out in another tab —
  // see customFetch and src/lib/auth.ts): go back to the login form.
  useEffect(() => {
    if (token === null && location.pathname !== LOGIN_PATH) {
      navigate({ to: LOGIN_PATH });
    }
  }, [token, location.pathname, navigate]);

  if (token === null) {
    return (
      <main className="flex min-h-svh flex-col px-4 pt-safe pb-safe md:p-6">
        <Outlet />
      </main>
    );
  }

  return (
    <div className="flex min-h-svh flex-col">
      {/* Sticky so the account controls stay reachable while a long list scrolls. */}
      <header className="sticky top-0 z-40 border-b bg-background/90 pt-safe backdrop-blur">
        <div
          className={cn(
            "mx-auto flex w-full items-center gap-1 px-4 py-2 md:px-6",
            width,
          )}
        >
          <span className="font-semibold md:mr-4">dx</span>
          <nav className="hidden items-center gap-1 md:flex">
            {navItems.map((item) => (
              <Button key={item.to} variant="ghost" asChild>
                <Link
                  to={item.to}
                  activeOptions={{ exact: item.exact }}
                  activeProps={{
                    className: "bg-accent text-accent-foreground",
                  }}
                >
                  {item.label}
                </Link>
              </Button>
            ))}
          </nav>
          <UserMenu />
        </div>
      </header>
      {/* The bottom padding clears the fixed tab bar; from md up there is none. */}
      <main
        className={cn(
          "mx-auto w-full flex-1 px-4 pt-6 pb-[calc(4.5rem+env(safe-area-inset-bottom))] md:px-6 md:py-8",
          width,
        )}
      >
        <Outlet />
      </main>
      <TabBar />
    </div>
  );
}

function TabBar(): JSX.Element {
  return (
    <nav className="fixed inset-x-0 bottom-0 z-40 border-t bg-background pb-safe md:hidden">
      <ul className="grid grid-cols-6">
        {navItems.map((item) => {
          const Icon = item.icon;
          return (
            <li key={item.to}>
              <Link
                to={item.to}
                activeOptions={{ exact: item.exact }}
                activeProps={{ className: "text-foreground" }}
                className="flex h-14 flex-col items-center justify-center gap-1 text-[0.625rem] text-muted-foreground"
              >
                <Icon className="size-5" aria-hidden="true" />
                {item.label}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}

function UserMenu(): JSX.Element {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const me = useGetCurrentUser();

  function logout(): void {
    const refresh = getRefreshToken();
    if (refresh !== null) {
      // Revoke the session server-side; best effort — locally it ends either way.
      logoutRequest({ refresh_token: refresh }).catch((): void => undefined);
    }
    setTokens(null);
    queryClient.clear(); // nothing of the previous user may survive in the cache
    navigate({ to: LOGIN_PATH });
  }

  return (
    <div className="ml-auto flex min-w-0 items-center gap-2">
      {me.data && (
        <span className="truncate text-muted-foreground text-sm">
          {me.data.username}
        </span>
      )}
      <Button variant="ghost" size="sm" onClick={logout}>
        Log out
      </Button>
    </div>
  );
}

function NotFound(): JSX.Element {
  return (
    <div className="flex flex-col gap-2">
      <h1 className="font-semibold text-xl">Page not found</h1>
      <p className="text-muted-foreground">
        <Link to="/" className="underline">
          Back to home
        </Link>
      </p>
    </div>
  );
}
