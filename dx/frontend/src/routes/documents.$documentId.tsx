import type { Query } from "@tanstack/react-query";
import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import type { JSX } from "react";
import {
  getGetDocumentContentQueryKey,
  getGetDocumentQueryKey,
  getGetDocumentTimelineQueryKey,
  getListExtractionsQueryKey,
  useGetDocument,
  useGetDocumentContent,
  useGetDocumentTimeline,
  useListExtractions,
  useReextractDocument,
} from "@/api/documents/documents";
import type {
  ConfStats,
  ContentOut,
  DateOut,
  DocumentOut,
  ExtractionOut,
  ExtractionStatus,
  TimelineEntryOut,
} from "@/api/model";
import { Button } from "@/components/ui/button";
import { errorMessage } from "@/lib/custom-fetch";
import { formatBytes } from "@/lib/format";
import { StatusBadge } from "./documents.index";

export const Route = createFileRoute("/documents/$documentId")({
  component: DocumentPage,
});

/** A run still in progress: poll until it settles (the task runs in the worker). */
function isActive(status: ExtractionStatus | null | undefined): boolean {
  return status === "pending" || status === "running";
}

const POLL_MS = 2000;

/** The ten uniform confidence buckets of `ConfStats.hist`, as labels. */
const BUCKETS: readonly string[] = Array.from(
  { length: 10 },
  (_, index): string => `${index * 10}–${(index + 1) * 10}%`,
);

function DocumentPage(): JSX.Element {
  const { documentId } = Route.useParams();
  const queryClient = useQueryClient();
  const document = useGetDocument(documentId, {
    query: {
      queryKey: getGetDocumentQueryKey(documentId),
      refetchInterval: (query: Query<DocumentOut>): number | false =>
        isActive(query.state.data?.status) ? POLL_MS : false,
    },
  });
  const content = useGetDocumentContent(documentId, {
    query: {
      queryKey: getGetDocumentContentQueryKey(documentId),
      refetchInterval: (query: Query<ContentOut>): number | false =>
        isActive(query.state.data?.status) ? POLL_MS : false,
    },
  });
  const extractions = useListExtractions(documentId, {
    query: {
      queryKey: getListExtractionsQueryKey(documentId),
      refetchInterval: (query: Query<ExtractionOut[]>): number | false =>
        query.state.data?.some((run) => isActive(run.status)) ? POLL_MS : false,
    },
  });
  const timeline = useGetDocumentTimeline(documentId, undefined, {
    query: { queryKey: getGetDocumentTimelineQueryKey(documentId) },
  });
  const reextract = useReextractDocument({
    mutation: {
      onSuccess: (): Promise<void> =>
        Promise.all([
          queryClient.invalidateQueries({
            queryKey: getGetDocumentTimelineQueryKey(documentId),
          }),
          queryClient.invalidateQueries({
            queryKey: getGetDocumentQueryKey(documentId),
          }),
          queryClient.invalidateQueries({
            queryKey: getGetDocumentContentQueryKey(documentId),
          }),
          queryClient.invalidateQueries({
            queryKey: getListExtractionsQueryKey(documentId),
          }),
        ]).then((): void => undefined),
    },
  });

  return (
    <div className="flex flex-col gap-6 md:gap-8">
      <div className="flex flex-col gap-1">
        <p className="text-sm">
          <Link to="/documents" className="underline underline-offset-4">
            Documents
          </Link>
        </p>
        <h1 className="break-all font-semibold text-2xl">
          {document.data?.title ?? "Document"}
        </h1>
        {document.isSuccess && (
          <p className="flex flex-wrap items-center gap-2 text-muted-foreground text-sm">
            <span>{document.data.mime_type || "unknown type"}</span>
            <span>{formatBytes(document.data.size)}</span>
            <StatusBadge status={document.data.status} />
            {document.data.page_count > 0 && (
              <span>{document.data.page_count} pages</span>
            )}
            {document.data.date && <DateLabel date={document.data.date} />}
            <Button
              variant="ghost"
              size="sm"
              onClick={(): void => reextract.mutate({ documentId })}
              disabled={reextract.isPending || isActive(document.data.status)}
            >
              {reextract.isPending ? "Queueing..." : "Re-extract"}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={(): void =>
                reextract.mutate({ documentId, params: { from_raw: true } })
              }
              disabled={reextract.isPending || isActive(document.data.status)}
              title="Rebuild the snapshot from the stored extractor output (re-dating, no extraction cost)"
            >
              Rebuild
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <a
                href={document.data.download_url}
                download={document.data.title}
              >
                Download
              </a>
            </Button>
            <Button variant="ghost" size="sm" asChild>
              <Link
                to="/history/$resource/$objectId"
                params={{ resource: "document", objectId: documentId }}
              >
                History
              </Link>
            </Button>
          </p>
        )}
        {document.isError && (
          <p className="text-destructive">
            Failed to load the document: {errorMessage(document.error)}
          </p>
        )}
        {reextract.isError && (
          <p className="text-destructive text-sm">
            Re-extraction refused: {errorMessage(reextract.error)}
          </p>
        )}
      </div>

      {content.isError && (
        <p className="text-destructive">
          Failed to load the content: {errorMessage(content.error)}
        </p>
      )}
      {content.isSuccess && <Snapshot content={content.data} />}

      {timeline.isSuccess && timeline.data.length > 0 && (
        <Timeline entries={timeline.data} />
      )}

      {extractions.isSuccess && extractions.data.length > 0 && (
        <ExtractionRuns runs={extractions.data} />
      )}
    </div>
  );
}

function Snapshot({ content }: { content: ContentOut }): JSX.Element {
  if (content.status === null) {
    return (
      <p className="text-muted-foreground">
        No extractor handles this kind of file, so there is nothing to show
        beyond the download.
      </p>
    );
  }
  if (content.html === "") {
    return (
      <p className="text-muted-foreground">
        {isActive(content.status)
          ? "Extraction is running; this page refreshes by itself."
          : content.extraction?.error
            ? `Extraction failed: ${content.extraction.error}`
            : "The extraction produced no content."}
      </p>
    );
  }
  return (
    <div className="grid gap-6 lg:grid-cols-[16rem_1fr]">
      <aside className="flex flex-col gap-4 text-sm">
        <Confidence stats={content.confidence} />
        {content.outline.length > 0 && (
          <nav className="flex flex-col gap-1">
            <h2 className="font-medium">Outline</h2>
            <ol className="flex flex-col gap-0.5 text-muted-foreground">
              {content.outline.map((entry) => (
                <li
                  key={entry.nid}
                  style={{
                    paddingLeft: `${Math.max((entry.level ?? 1) - 1, 0) * 0.75}rem`,
                  }}
                >
                  {entry.title}
                </li>
              ))}
            </ol>
          </nav>
        )}
      </aside>
      <article
        className="document-html min-w-0 rounded-md border p-4"
        // biome-ignore lint/security/noDangerouslySetInnerHtml: sanitized server-side by nh3 (apps/documents/snapshot.py); the tag allowlist is the whole vocabulary
        dangerouslySetInnerHTML={{ __html: content.html }}
      />
    </div>
  );
}

/** "May 12–20, 1943 (interpolated, 0.60)" — from the stored fields alone. */
function DateLabel({ date }: { date: DateOut }): JSX.Element {
  return (
    <span
      title={`EDTF ${date.edtf}`}
      className="rounded-full border px-2 py-0.5 text-xs"
    >
      {date.display}
    </span>
  );
}

/** The dated nodes, earliest first: what a reader scrolls and a reviewer checks. */
function Timeline({ entries }: { entries: TimelineEntryOut[] }): JSX.Element {
  return (
    <section className="flex flex-col gap-2 text-sm">
      <h2 className="font-medium">Timeline</h2>
      <ol className="flex flex-col gap-1">
        {entries.map((entry) => (
          <li key={entry.nid} className="flex flex-wrap items-baseline gap-2">
            <span className="whitespace-nowrap font-medium">
              {entry.date.display}
            </span>
            <span className="text-muted-foreground text-xs">
              {entry.date.source}
              {entry.date.conf !== null && ` · ${entry.date.conf.toFixed(2)}`}
              {entry.pages.length > 0 && ` · p. ${entry.pages.join(", ")}`}
            </span>
            <span className="text-muted-foreground">{entry.excerpt}</span>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Confidence({ stats }: { stats: ConfStats | null }): JSX.Element {
  if (stats === null) {
    return (
      <p className="text-muted-foreground">
        Confidence: none recorded (born-digital text, no OCR).
      </p>
    );
  }
  const mean = stats.sum / stats.n;
  return (
    <div className="flex flex-col gap-1">
      <h2 className="font-medium">Confidence</h2>
      <p className="text-muted-foreground">
        mean {(mean * 100).toFixed(0)}% over {stats.n.toLocaleString()} words,
        min {(stats.min * 100).toFixed(0)}%
      </p>
      <div
        className="flex h-8 items-end gap-px"
        role="img"
        aria-label="Confidence histogram"
      >
        {BUCKETS.map((bucket, index) => {
          const count = stats.hist[index] ?? 0;
          const height =
            stats.n > 0
              ? Math.max((count / stats.n) * 100, count > 0 ? 6 : 0)
              : 0;
          return (
            <div
              key={bucket}
              className="flex-1 bg-primary/60"
              style={{ height: `${height}%` }}
              title={`${bucket}: ${count}`}
            />
          );
        })}
      </div>
    </div>
  );
}

function ExtractionRuns({ runs }: { runs: ExtractionOut[] }): JSX.Element {
  return (
    <section className="flex flex-col gap-2 text-sm">
      <h2 className="font-medium">Extraction runs</h2>
      <ul className="flex flex-col gap-1">
        {runs.map((run) => (
          <li key={run.id} className="flex flex-wrap items-center gap-2">
            <StatusBadge status={run.status} />
            <span>{run.extractor}</span>
            <span className="text-muted-foreground">
              {new Date(run.created).toLocaleString()}
            </span>
            {run.is_current && (
              <span className="text-muted-foreground text-xs">current</span>
            )}
            {run.error && (
              <span className="text-destructive text-xs">{run.error}</span>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
