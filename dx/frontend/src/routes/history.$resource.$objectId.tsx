import { createFileRoute, Link } from "@tanstack/react-router";
import type { JSX } from "react";
import { useGetHistory } from "@/api/history/history";
import type { ChangeOut, RevisionGroupOut, RevisionOut } from "@/api/model";
import { errorMessage } from "@/lib/custom-fetch";

export const Route = createFileRoute("/history/$resource/$objectId")({
  component: HistoryPage,
});

/** Where a revision came from. The backend only records non-identifying labels. */
const SOURCE_LABEL: Record<string, string> = {
  api: "Web request",
  task: "Background task",
  command: "Management command",
  unknown: "No context recorded",
};

function HistoryPage(): JSX.Element {
  const { resource, objectId } = Route.useParams();
  const history = useGetHistory(resource, objectId);

  return (
    <div className="flex flex-col gap-6 md:gap-8">
      <div className="flex flex-col gap-1">
        <h1 className="font-semibold text-2xl">
          History{history.data ? ` · ${history.data.model}` : ""}
        </h1>
        <p className="break-all font-mono text-muted-foreground text-xs">
          {objectId}
        </p>
        {history.data && (
          <p className="text-muted-foreground text-sm">
            Currently at version {history.data.current_version}. Every write is
            captured by a database trigger, so bulk updates and raw SQL appear
            here too.
          </p>
        )}
      </div>

      {history.isPending && (
        <p className="text-muted-foreground">Loading history...</p>
      )}
      {history.isError && (
        <p className="text-destructive">
          Failed to load history: {errorMessage(history.error)}
        </p>
      )}
      {history.isSuccess && history.data.groups.length === 0 && (
        <p className="text-muted-foreground">No versions recorded.</p>
      )}
      {history.isSuccess && (
        <ol className="flex flex-col gap-4">
          {history.data.groups.map((group, index) => (
            <RevisionGroupCard
              key={group.context_id ?? `orphan-${index}`}
              group={group}
            />
          ))}
        </ol>
      )}
    </div>
  );
}

function RevisionGroupCard({
  group,
}: {
  group: RevisionGroupOut;
}): JSX.Element {
  return (
    <li className="flex flex-col gap-3 rounded-lg border p-3 sm:p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-medium">
          {SOURCE_LABEL[group.source] ?? group.source}
        </span>
        <time className="text-muted-foreground text-sm" dateTime={group.at}>
          {new Date(group.at).toLocaleString()}
        </time>
      </div>
      {/* One save can span several tables; the context id is what ties them together. */}
      {group.revisions.map((revision) => (
        <Revision key={revision.pgh_id} revision={revision} />
      ))}
    </li>
  );
}

/** What happened to a child row, in the words a reader wants: added, removed, changed. */
function childAction(revision: RevisionOut): string {
  if (revision.deleted) return "removed";
  return revision.label === "insert" ? "added" : "changed";
}

function Revision({ revision }: { revision: RevisionOut }): JSX.Element {
  return (
    <div className="flex flex-col gap-2 border-l-2 pl-3 sm:pl-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span className="font-medium">
          {revision.model} v{revision.version}
        </span>
        {/* A child row written in the same save — a tag link, say. It is described rather than
            diffed: its own columns are foreign keys, and UUIDs read worse than names. */}
        {revision.is_related ? (
          <span className="text-muted-foreground">
            {childAction(revision)}
            {revision.description && ` “${revision.description}”`}
          </span>
        ) : (
          <span className="rounded bg-muted px-2 py-0.5 text-muted-foreground text-xs">
            {revision.label}
          </span>
        )}
        {revision.deleted && !revision.is_related && (
          <span className="rounded bg-destructive/10 px-2 py-0.5 text-destructive text-xs">
            deleted
          </span>
        )}
      </div>

      {!revision.schema_known && (
        <p className="text-muted-foreground text-sm">
          Written under schema {revision.schema_tag}, which this build does not
          know. The stored values are shown without a diff rather than compared
          against fields that may not have existed yet.
        </p>
      )}

      {revision.changes.length > 0 && (
        <dl className="grid gap-1 text-sm">
          {revision.changes.map((change) => (
            <Change key={change.field} change={change} />
          ))}
        </dl>
      )}

      {revision.unknown_fields.length > 0 && revision.schema_known && (
        <p className="text-muted-foreground text-xs">
          Not tracked at this version: {revision.unknown_fields.join(", ")}
        </p>
      )}

      {Object.keys(revision.archived).length > 0 && (
        <div className="text-xs">
          <span className="text-muted-foreground">Archived fields: </span>
          {Object.entries(revision.archived).map(([field, value]) => (
            <span key={field} className="mr-3 font-mono">
              {field}={value}
            </span>
          ))}
        </div>
      )}

      {revision.sources.length > 0 && (
        <div className="flex flex-col gap-1 text-sm">
          <span className="text-muted-foreground text-xs">Derived from</span>
          {revision.sources.map((source) => (
            <Link
              key={source.pgh_id}
              to="/history/$resource/$objectId"
              params={{
                resource: source.model.toLowerCase(),
                objectId: source.object_id,
              }}
              className="underline underline-offset-4"
            >
              {source.model} “{source.label}” v{source.version}
              {source.is_stale && (
                <span className="ml-2 text-muted-foreground text-xs">
                  (superseded since)
                </span>
              )}
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}

function Change({ change }: { change: ChangeOut }): JSX.Element {
  return (
    <div className="grid items-baseline gap-x-2 sm:grid-cols-[10rem_1fr]">
      <dt className="truncate font-mono text-muted-foreground text-xs">
        {change.field}
      </dt>
      <dd className="flex flex-wrap items-baseline gap-2 break-words">
        {change.old !== null && change.old !== "" && (
          <span className="text-muted-foreground line-through">
            {change.old}
          </span>
        )}
        <span>
          {change.new === null || change.new === "" ? "—" : change.new}
        </span>
      </dd>
    </div>
  );
}
