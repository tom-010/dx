import { type Query, useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
import { type JSX, useEffect, useState } from "react";
import type { TaskOut } from "@/api/model";
import {
  getGetTaskQueryKey,
  useGetTask,
  useRunAdd,
  useRunCount,
  useRunDatasetSummary,
  useRunFail,
} from "@/api/tasks/tasks";
import { GetTaskResponse } from "@/api/zod/tasks/tasks";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { apiUrl, errorMessage } from "@/lib/custom-fetch";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/tasks")({
  component: TasksPage,
});

type Run = { label: string; initial: TaskOut; startedAt: Date };

// Only used when the SSE stream cannot be opened (see useTaskStream).
const FALLBACK_POLL_INTERVAL_MS = 500;

function TasksPage(): JSX.Element {
  const [runs, setRuns] = useState<Run[]>([]);
  function track(label: string): (task: TaskOut) => void {
    return (task: TaskOut): void =>
      setRuns((previous) => [
        { label, initial: task, startedAt: new Date() },
        ...previous,
      ]);
  }

  // Generated from the OpenAPI spec (apps/core/api.py, tag "tasks").
  const add = useRunAdd({ mutation: { onSuccess: track("add(2, 3)") } });
  const count = useRunCount({
    mutation: { onSuccess: track("count to 10 (0.5 s per step)") },
  });
  const summary = useRunDatasetSummary({
    mutation: { onSuccess: track("dataset summary") },
  });
  const fail = useRunFail({
    mutation: { onSuccess: track("fail on purpose") },
  });
  const failedToStart = [add, count, summary, fail].find((m) => m.isError);

  return (
    <div className="flex flex-col gap-6 md:gap-8">
      <div>
        <h1 className="font-semibold text-2xl">Background tasks</h1>
        <p className="text-muted-foreground">
          Sample Celery tasks from <code>apps/core/tasks.py</code>. They are
          queued for the worker (<code>./scripts/celery.sh</code>) and their
          progress arrives live over Server-Sent Events (
          <code>GET /api/tasks/:id/events</code>). With{" "}
          <code>CELERY_EAGER=true</code> they finish inside the request instead.
        </p>
      </div>

      <div className="flex flex-wrap gap-2">
        <Button
          onClick={(): void => add.mutate({ data: { a: 2, b: 3 } })}
          disabled={add.isPending}
        >
          Add 2 + 3
        </Button>
        <Button
          onClick={(): void => count.mutate({ data: { n: 10, delay: 0.5 } })}
          disabled={count.isPending}
        >
          Count to 10 (slow)
        </Button>
        <Button
          onClick={(): void => summary.mutate()}
          disabled={summary.isPending}
        >
          Dataset summary
        </Button>
        <Button
          variant="outline"
          onClick={(): void => fail.mutate()}
          disabled={fail.isPending}
        >
          Fail on purpose
        </Button>
      </div>
      {failedToStart && (
        <p className="text-destructive text-sm">
          Could not start task: {errorMessage(failedToStart.error)}
        </p>
      )}

      <section className="flex flex-col gap-3">
        {runs.length === 0 && (
          <p className="text-muted-foreground">
            No tasks started yet. Click a button above.
          </p>
        )}
        {runs.map((run) => (
          <TaskRun key={run.initial.id} run={run} />
        ))}
      </section>
    </div>
  );
}

/**
 * Follows a task over SSE: every `status` event is written into the query cache, so
 * `useGetTask` for the same id sees it without a request. Returns true when the stream is
 * gone for good (e.g. an expired link) and the caller should poll instead.
 */
function useTaskStream(initial: TaskOut): boolean {
  const queryClient = useQueryClient();
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (initial.ready) return;
    // EventSource cannot send the bearer header; the URL is signed instead (see the backend).
    const source = new EventSource(apiUrl(initial.stream_url));
    source.addEventListener("status", (event: MessageEvent<string>) => {
      const parsed = GetTaskResponse.safeParse(JSON.parse(event.data));
      if (!parsed.success) return;
      queryClient.setQueryData(getGetTaskQueryKey(initial.id), parsed.data);
      // The server closes the stream once the task is ready; closing here as well stops
      // EventSource from reconnecting.
      if (parsed.data.ready) source.close();
    });
    source.onerror = (): void => {
      // CONNECTING = the browser retries by itself (the server closes idle streams after a
      // timeout); CLOSED = it gave up (non-200 response), so fall back to polling.
      if (source.readyState === EventSource.CLOSED) setFailed(true);
    };
    return () => source.close();
  }, [initial, queryClient]);

  return failed;
}

function TaskRun({ run }: { run: Run }): JSX.Element {
  const streamFailed = useTaskStream(run.initial);
  // Starts from the snapshot the POST returned; updates arrive via the stream, polling is
  // only the fallback.
  const task = useGetTask(run.initial.id, {
    query: {
      queryKey: getGetTaskQueryKey(run.initial.id),
      initialData: run.initial,
      staleTime: Number.POSITIVE_INFINITY,
      refetchInterval: (query: Query<TaskOut>): number | false =>
        streamFailed && !query.state.data?.ready
          ? FALLBACK_POLL_INTERVAL_MS
          : false,
    },
  });
  const status = task.data ?? run.initial;
  const percent =
    status.progress && status.progress.total > 0
      ? Math.round((100 * status.progress.current) / status.progress.total)
      : null;

  return (
    <Card>
      <CardHeader>
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="min-w-0 truncate">{run.label}</CardTitle>
          <StateBadge state={status.state} />
        </div>
        <CardDescription className="break-all">
          {run.startedAt.toLocaleTimeString()} · id{" "}
          <code className="text-xs">{status.id}</code>
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-2 text-sm">
        {percent !== null && (
          <div className="flex items-center gap-3">
            <div className="h-2 flex-1 overflow-hidden rounded bg-muted">
              <div
                className="h-full bg-primary transition-[width]"
                style={{ width: `${percent}%` }}
              />
            </div>
            <span className="tabular-nums">
              {status.progress?.current}/{status.progress?.total}
            </span>
          </div>
        )}
        {status.state === "SUCCESS" && (
          <p>
            Result: <code>{JSON.stringify(status.result)}</code>
          </p>
        )}
        {status.error && <p className="text-destructive">{status.error}</p>}
        {task.isError && (
          <p className="text-destructive">
            Polling failed: {errorMessage(task.error)}
          </p>
        )}
      </CardContent>
    </Card>
  );
}

function StateBadge({ state }: { state: TaskOut["state"] }): JSX.Element {
  const tone =
    state === "SUCCESS"
      ? "bg-emerald-100 text-emerald-900 dark:bg-emerald-900 dark:text-emerald-100"
      : state === "FAILURE"
        ? "bg-destructive/15 text-destructive"
        : "bg-muted text-muted-foreground";
  return (
    <span
      className={cn(
        "rounded-full px-2 py-0.5 font-medium text-xs tabular-nums",
        tone,
      )}
    >
      {state}
    </span>
  );
}
