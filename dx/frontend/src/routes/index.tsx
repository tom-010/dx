import { createFileRoute } from "@tanstack/react-router";
import type { JSX } from "react";
import { useHealthCheck, useReady } from "@/api/core/core";
import type { ReadyOut } from "@/api/model";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { ApiError, errorMessage } from "@/lib/custom-fetch";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/")({
  component: HomePage,
});

function HomePage(): JSX.Element {
  // Generated from the OpenAPI spec (orval): typed fetchers + TanStack Query hooks.
  const health = useHealthCheck();
  const ready = useReady();

  // `GET /api/ready` answers 503 with the same body when a check fails; customFetch throws
  // an ApiError for non-2xx, so the checks are read from the error in that case.
  const readyError: unknown = ready.error;
  const readiness: ReadyOut | null =
    ready.data ??
    (readyError instanceof ApiError && isReadyBody(readyError.detail)
      ? readyError.detail
      : null);

  return (
    <div className="flex flex-col gap-6">
      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Liveness</CardTitle>
            <CardDescription>
              <code>GET /api/health</code> — the process answers
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <pre className="overflow-x-auto rounded-md bg-muted p-3 text-sm">
              {health.isPending && "Loading..."}
              {health.isError && `Error: ${errorMessage(health.error)}`}
              {health.isSuccess && JSON.stringify(health.data, null, 2)}
            </pre>
            <Button
              onClick={(): void => {
                void health.refetch();
              }}
              disabled={health.isFetching}
            >
              {health.isFetching ? "Refreshing..." : "Refresh"}
            </Button>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Readiness</CardTitle>
            <CardDescription>
              <code>GET /api/ready</code> — database, migrations, broker,
              storage
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            {ready.isPending && (
              <p className="text-muted-foreground text-sm">Loading...</p>
            )}
            {readiness ? (
              <ul className="flex flex-col gap-1 text-sm">
                {readiness.checks.map((check) => (
                  <li
                    key={check.name}
                    className="flex flex-wrap items-baseline gap-x-2"
                  >
                    <span
                      className={cn(
                        "font-mono",
                        check.ok ? "text-green-600" : "text-destructive",
                      )}
                    >
                      {check.ok ? "ok " : "FAIL"}
                    </span>
                    <span className="font-medium">{check.name}</span>
                    {check.detail && (
                      <span className="text-muted-foreground">
                        {check.detail}
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            ) : (
              ready.isError && (
                <p className="text-destructive text-sm">
                  Error: {errorMessage(ready.error)}
                </p>
              )
            )}
            <Button
              onClick={(): void => {
                void ready.refetch();
              }}
              disabled={ready.isFetching}
            >
              {ready.isFetching ? "Checking..." : "Check again"}
            </Button>
          </CardContent>
        </Card>
      </div>

      <p className="max-w-prose text-muted-foreground text-sm">
        Every file in <code>src/routes/</code> is compiled into its own
        JavaScript chunk. The first visit downloads only the app shell plus the
        route you opened; other routes (and heavy libraries used inside event
        handlers) are fetched on demand when you navigate or click.
      </p>
    </div>
  );
}

function isReadyBody(value: unknown): value is ReadyOut {
  return (
    typeof value === "object" &&
    value !== null &&
    "checks" in value &&
    Array.isArray((value as { checks: unknown }).checks)
  );
}
