import { useQueryClient } from "@tanstack/react-query";
import {
  createRootRoute,
  Link,
  Outlet,
  type ParsedLocation,
  redirect,
  useLocation,
  useNavigate,
} from "@tanstack/react-router";
import { type JSX, useEffect } from "react";
import { logout as logoutRequest, useGetCurrentUser } from "@/api/auth/auth";
import { Button } from "@/components/ui/button";
import {
  getAccessToken,
  getRefreshToken,
  setTokens,
  useAccessToken,
} from "@/lib/auth";

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

const navItems = [
  { to: "/", label: "Home", exact: true },
  { to: "/datasets", label: "Datasets", exact: false },
  { to: "/documents", label: "Documents", exact: false },
  { to: "/gallery", label: "Gallery", exact: false },
  { to: "/tasks", label: "Tasks", exact: false },
] as const;

function RootLayout(): JSX.Element {
  const token = useAccessToken();
  const location = useLocation();
  const navigate = useNavigate();

  // The session can end while a page is open (refresh rejected, logged out in another tab —
  // see customFetch and src/lib/auth.ts): go back to the login form.
  useEffect(() => {
    if (token === null && location.pathname !== LOGIN_PATH) {
      navigate({ to: LOGIN_PATH });
    }
  }, [token, location.pathname, navigate]);

  if (token === null) {
    return (
      <main className="flex min-h-svh flex-col p-6">
        <Outlet />
      </main>
    );
  }

  return (
    <div className="flex min-h-svh flex-col">
      <header className="border-b">
        <nav className="mx-auto flex w-full max-w-5xl items-center gap-1 px-6 py-2">
          <span className="mr-4 font-semibold">dx</span>
          {navItems.map((item) => (
            <Button key={item.to} variant="ghost" asChild>
              <Link
                to={item.to}
                activeOptions={{ exact: item.exact }}
                activeProps={{ className: "bg-accent text-accent-foreground" }}
              >
                {item.label}
              </Link>
            </Button>
          ))}
          <UserMenu />
        </nav>
      </header>
      <main className="mx-auto w-full max-w-5xl flex-1 p-6">
        <Outlet />
      </main>
    </div>
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
    <div className="ml-auto flex items-center gap-2">
      {me.data && (
        <span className="text-muted-foreground text-sm">
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
