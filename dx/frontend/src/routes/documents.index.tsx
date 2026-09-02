import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import type { FormEvent, JSX } from "react";
import {
  getListDatasetsQueryKey,
  useImportDatasetFromDocument,
} from "@/api/datasets/datasets";
import {
  getListDocumentsQueryKey,
  getSearchDocumentsQueryKey,
  useDeleteDocument,
  useListDocuments,
  useSearchDocuments,
  useUploadDocuments,
} from "@/api/documents/documents";
import type { DocumentOut, ExtractionStatus, SearchHitOut } from "@/api/model";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { UploadForm } from "@/components/upload-form";
import { errorMessage } from "@/lib/custom-fetch";
import { formatBytes } from "@/lib/format";

type DocumentsSearch = { q?: string; period?: string };

function optional(value: unknown): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined;
}

export const Route = createFileRoute("/documents/")({
  component: DocumentsPage,
  // Both filters live in the URL (F5-ability): `?q=` drives the text search, `?period=`
  // (EDTF, e.g. 1943-05) keeps the documents with information from that period.
  validateSearch: (search: Record<string, unknown>): DocumentsSearch => ({
    q: optional(search.q),
    period: optional(search.period),
  }),
});

function DocumentsPage(): JSX.Element {
  const queryClient = useQueryClient();
  const { q = "", period = "" } = Route.useSearch();
  const navigate = Route.useNavigate();
  // Generated from openschema.json: list query + upload/delete mutations.
  const documents = useListDocuments(period ? { period } : undefined);
  const search = useSearchDocuments(
    { q },
    {
      query: {
        queryKey: getSearchDocumentsQueryKey({ q }),
        enabled: q.length > 0,
      },
    },
  );
  const invalidateList = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: getListDocumentsQueryKey() });
  const setFilters = (next: DocumentsSearch): void => {
    void navigate({ search: { q: next.q, period: next.period } });
  };
  const upload = useUploadDocuments({
    mutation: { onSuccess: invalidateList },
  });
  const remove = useDeleteDocument({
    mutation: { onSuccess: invalidateList },
  });
  // Importing builds a dataset from this document and records where it came from; the datasets
  // list has to be refetched because a new one appeared there.
  const importDocument = useImportDatasetFromDocument({
    mutation: {
      onSuccess: (): Promise<void> =>
        queryClient.invalidateQueries({ queryKey: getListDatasetsQueryKey() }),
    },
  });

  return (
    <div className="flex flex-col gap-6 md:gap-8">
      <h1 className="font-semibold text-2xl">Documents</h1>

      <UploadForm
        title="Upload documents"
        onUpload={(files: File[]): void => upload.mutate({ data: { files } })}
        pending={upload.isPending}
        error={upload.isError ? errorMessage(upload.error) : null}
        uploadedCount={upload.isSuccess ? upload.data.length : null}
      />

      <form
        className="flex flex-wrap gap-2"
        onSubmit={(event: FormEvent<HTMLFormElement>): void => {
          event.preventDefault();
          const form = new FormData(event.currentTarget);
          const query = form.get("q");
          const when = form.get("period");
          setFilters({
            q: optional(typeof query === "string" ? query.trim() : ""),
            period: optional(typeof when === "string" ? when.trim() : ""),
          });
        }}
      >
        <Input
          name="q"
          defaultValue={q}
          placeholder="Search the extracted text..."
          aria-label="Search documents"
          className="min-w-48 flex-1"
        />
        <Input
          name="period"
          defaultValue={period}
          placeholder="Period (EDTF: 1943, 1943-05, 1940/1945)"
          aria-label="Filter by period"
          className="min-w-48 flex-1"
        />
        <Button type="submit" variant="secondary">
          Apply
        </Button>
      </form>
      {q.length > 0 && (
        <SearchResults
          query={q}
          hits={search.data ?? []}
          pending={search.isPending}
          error={search.isError ? errorMessage(search.error) : null}
        />
      )}

      <section className="flex flex-col gap-2">
        {documents.isPending && (
          <p className="text-muted-foreground">Loading documents...</p>
        )}
        {documents.isError && (
          <p className="text-destructive">
            Failed to load documents: {errorMessage(documents.error)}
          </p>
        )}
        {period && documents.isSuccess && (
          <p className="text-muted-foreground text-sm">
            Documents with information from{" "}
            <span className="font-mono">{period}</span>: {documents.data.count}
          </p>
        )}
        {importDocument.isError && (
          <p className="text-destructive text-sm">
            Import failed: {errorMessage(importDocument.error)}
          </p>
        )}
        {importDocument.isSuccess && (
          <p className="text-muted-foreground text-sm">
            Imported <strong>{importDocument.data.name}</strong> —{" "}
            {importDocument.data.row_count.toLocaleString()} rows.{" "}
            <Link
              to="/history/$resource/$objectId"
              params={{ resource: "dataset", objectId: importDocument.data.id }}
              className="underline underline-offset-4"
            >
              See what it was built from
            </Link>
          </p>
        )}
        {documents.isSuccess && (
          <DocumentTable
            documents={documents.data.items}
            onImport={(documentId: string): void =>
              importDocument.mutate({ data: { document_id: documentId } })
            }
            importingId={
              importDocument.isPending
                ? importDocument.variables.data.document_id
                : null
            }
            onDelete={(documentId: string): void =>
              remove.mutate({ documentId })
            }
            deletingId={remove.isPending ? remove.variables.documentId : null}
          />
        )}
        {remove.isError && (
          <p className="text-destructive text-sm">
            Delete failed: {errorMessage(remove.error)}
          </p>
        )}
      </section>
    </div>
  );
}

type SearchResultsProps = {
  query: string;
  hits: SearchHitOut[];
  pending: boolean;
  error: string | null;
};

function SearchResults({
  query,
  hits,
  pending,
  error,
}: SearchResultsProps): JSX.Element {
  return (
    <section className="flex flex-col gap-2 rounded-md border p-3">
      <h2 className="font-medium text-sm">
        Results for <span className="font-mono">{query}</span>
      </h2>
      {pending && <p className="text-muted-foreground text-sm">Searching...</p>}
      {error && (
        <p className="text-destructive text-sm">Search failed: {error}</p>
      )}
      {!pending && !error && hits.length === 0 && (
        <p className="text-muted-foreground text-sm">Nothing found.</p>
      )}
      <ul className="flex flex-col gap-2">
        {hits.map((hit) => (
          <li key={`${hit.document_id}-${hit.offset}`} className="text-sm">
            <Link
              to="/documents/$documentId"
              params={{ documentId: hit.document_id }}
              className="font-medium underline underline-offset-4"
            >
              {hit.title}
            </Link>
            {hit.node && (
              <span className="ml-2 text-muted-foreground text-xs">
                in &lt;{hit.node.tag}&gt; #{hit.node.nid}
                {hit.node.pages.length > 0 &&
                  ` · page ${hit.node.pages.join(", ")}`}
              </span>
            )}
            <p className="text-muted-foreground">{hit.snippet}</p>
          </li>
        ))}
      </ul>
    </section>
  );
}

const STATUS_CLASS: Record<ExtractionStatus, string> = {
  pending: "bg-muted text-muted-foreground",
  running: "bg-blue-100 text-blue-900 dark:bg-blue-900 dark:text-blue-100",
  succeeded:
    "bg-green-100 text-green-900 dark:bg-green-900 dark:text-green-100",
  partial: "bg-amber-100 text-amber-900 dark:bg-amber-900 dark:text-amber-100",
  failed: "bg-red-100 text-red-900 dark:bg-red-900 dark:text-red-100",
};

/** The latest extraction's state; a document nothing extracts (an unknown type) has none. */
export function StatusBadge({
  status,
}: {
  status: ExtractionStatus | null;
}): JSX.Element {
  if (status === null) {
    return <span className="text-muted-foreground text-xs">no extractor</span>;
  }
  return (
    <span
      className={`rounded-full px-2 py-0.5 font-medium text-xs ${STATUS_CLASS[status]}`}
    >
      {status}
    </span>
  );
}

type DocumentTableProps = {
  documents: DocumentOut[];
  onDelete: (id: string) => void;
  deletingId: string | null;
  onImport: (id: string) => void;
  importingId: string | null;
};

/** Same shape rule as the dataset list: stacked below `lg`, a table from `lg` up. */
function DocumentTable({
  documents,
  onDelete,
  deletingId,
  onImport,
  importingId,
}: DocumentTableProps): JSX.Element {
  if (documents.length === 0) {
    return (
      <p className="text-muted-foreground">
        No documents yet. Upload some above.
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Title</TableHead>
          <TableHead className="hidden lg:table-cell">Type</TableHead>
          <TableHead className="hidden text-right lg:table-cell">
            Size
          </TableHead>
          <TableHead className="hidden lg:table-cell">Extraction</TableHead>
          <TableHead className="hidden lg:table-cell">Dated</TableHead>
          <TableHead className="hidden lg:table-cell">Uploaded</TableHead>
          <TableHead className="lg:w-56" />
        </TableRow>
      </TableHeader>
      <TableBody>
        {documents.map((document) => (
          <TableRow key={document.id}>
            <TableCell className="whitespace-normal break-all font-medium lg:whitespace-nowrap lg:break-normal">
              <Link
                to="/documents/$documentId"
                params={{ documentId: document.id }}
                className="underline-offset-4 hover:underline"
              >
                {document.title}
              </Link>
              <div className="mt-1 flex flex-wrap items-center gap-2 font-normal text-muted-foreground text-xs lg:hidden">
                <span>{formatBytes(document.size)}</span>
                {document.mime_type && <span>{document.mime_type}</span>}
                <StatusBadge status={document.status} />
                {document.date && <span>{document.date.display}</span>}
                <span>{new Date(document.created).toLocaleDateString()}</span>
              </div>
            </TableCell>
            <TableCell className="hidden text-muted-foreground lg:table-cell">
              {document.mime_type || "—"}
            </TableCell>
            <TableCell className="hidden text-right tabular-nums lg:table-cell">
              {formatBytes(document.size)}
            </TableCell>
            <TableCell className="hidden lg:table-cell">
              <StatusBadge status={document.status} />
              {document.page_count > 0 && (
                <span className="ml-2 text-muted-foreground text-xs">
                  {document.page_count} pages
                </span>
              )}
            </TableCell>
            <TableCell className="hidden text-muted-foreground lg:table-cell">
              {document.date ? (
                <span title={`${document.date.edtf} (${document.date.source})`}>
                  {document.date.display}
                </span>
              ) : (
                "—"
              )}
            </TableCell>
            <TableCell className="hidden text-muted-foreground lg:table-cell">
              {new Date(document.created).toLocaleString()}
            </TableCell>
            <TableCell>
              <div className="flex flex-wrap justify-end gap-1 lg:flex-nowrap">
                <Button variant="ghost" size="sm" asChild>
                  <a href={document.download_url} download={document.title}>
                    Download
                  </a>
                </Button>
                <Button asChild variant="ghost" size="sm">
                  <Link
                    to="/history/$resource/$objectId"
                    params={{ resource: "document", objectId: document.id }}
                  >
                    History
                  </Link>
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={(): void => onImport(document.id)}
                  disabled={importingId === document.id}
                  title="Create a dataset from this file"
                >
                  {importingId === document.id ? "Importing..." : "Import"}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={(): void => onDelete(document.id)}
                  disabled={deletingId === document.id}
                >
                  {deletingId === document.id ? "Deleting..." : "Delete"}
                </Button>
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
