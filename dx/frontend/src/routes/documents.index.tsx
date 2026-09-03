/**
 * The library: every document the reader owns, newest first, grouped by the month its
 * information comes from — the *end* of that span, so a record covering several years sits
 * where a reader looks for it rather than where it started (`timeOf`).
 *
 * The row shows the paper — a thumbnail of page one — because that is what a person
 * recognises; the file name is the caption, not the object. A status chip appears only when
 * something is off (still reading, partly read, unreadable): a document that came out fine
 * says so by saying nothing.
 *
 * Everything the eye can see is in the URL (`?q=&period=&add=&delete=`), so a reload, a back
 * button and a shared link all land on the same list, the same search and the same dialog.
 */
import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import {
  Download,
  FileText,
  GitBranch,
  MoreVertical,
  Plus,
  Search,
  Table2,
  Trash2,
} from "lucide-react";
import {
  type FormEvent,
  type JSX,
  type ReactNode,
  useEffect,
  useRef,
} from "react";
import {
  getListDatasetsQueryKey,
  useImportDatasetFromDocument,
} from "@/api/datasets/datasets";
import {
  getListDocumentsQueryKey,
  getSearchDocumentsQueryKey,
  useDeleteDocument,
  useListDocuments,
  useListUploadFormats,
  useSearchDocuments,
  useUploadDocuments,
} from "@/api/documents/documents";
import type {
  DocumentOut,
  ExtractionStatus,
  SearchHitOut,
  UploadFormatOut,
} from "@/api/model";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { UploadForm } from "@/components/upload-form";
import { apiUrl, errorMessage } from "@/lib/custom-fetch";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

type DocumentsSearch = {
  q?: string;
  period?: string;
  add?: boolean;
  delete?: string;
};

function optional(value: unknown): string | undefined {
  return typeof value === "string" && value !== "" ? value : undefined;
}

/** The `accept` attribute for the formats the API accepts — MIME types and extensions. */
function acceptOf(formats: UploadFormatOut[] | undefined): string | undefined {
  if (formats === undefined || formats.length === 0) return undefined;
  return formats
    .flatMap((format) => [format.mime_type, ...format.extensions])
    .join(",");
}

/** "Choose PDF files" — and just "Choose files" until the API has said which. */
function actionOf(formats: UploadFormatOut[] | undefined): string {
  const labels = (formats ?? []).map((format) => format.label);
  const last = labels.pop();
  if (last === undefined) return "Choose files";
  const named = labels.length === 0 ? last : `${labels.join(", ")} or ${last}`;
  return `Choose ${named} files`;
}

export const Route = createFileRoute("/documents/")({
  component: DocumentsPage,
  // Both filters live in the URL (F5-ability): `?q=` drives the text search, `?period=`
  // (EDTF, e.g. 1943-05) keeps the documents with information from that period. `?add=` opens
  // the upload panel and `?delete=<id>` the confirmation — a reload reopens either.
  validateSearch: (search: Record<string, unknown>): DocumentsSearch => ({
    q: optional(search.q),
    period: optional(search.period),
    add: search.add === true || search.add === "true" ? true : undefined,
    delete: optional(search.delete),
  }),
});

function DocumentsPage(): JSX.Element {
  const queryClient = useQueryClient();
  const { q = "", period = "", add, delete: deleting } = Route.useSearch();
  const navigate = Route.useNavigate();
  const searchBox = useRef<HTMLInputElement>(null);
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
  /** The one place this page's view state changes — and it changes the URL. */
  const show = (next: Partial<DocumentsSearch>): void => {
    void navigate({
      search: (previous: DocumentsSearch): DocumentsSearch => ({
        ...previous,
        ...next,
      }),
    });
  };
  const upload = useUploadDocuments({
    mutation: { onSuccess: invalidateList },
  });
  // What may be uploaded is the backend's list, not a constant here: the picker filters by
  // exactly what the server accepts (`documents.api.SUPPORTED_UPLOAD_FORMATS`).
  const formats = useListUploadFormats();
  const remove = useDeleteDocument({
    mutation: {
      onSuccess: (): void => {
        void invalidateList();
        show({ delete: undefined });
      },
    },
  });
  // Importing builds a dataset from this document and records where it came from; the datasets
  // list has to be refetched because a new one appeared there.
  const importDocument = useImportDatasetFromDocument({
    mutation: {
      onSuccess: (): Promise<void> =>
        queryClient.invalidateQueries({ queryKey: getListDatasetsQueryKey() }),
    },
  });

  // "/" jumps to the search box, the one shortcut worth having on a page that is a list.
  useEffect(() => {
    function handle(event: KeyboardEvent): void {
      const target = event.target;
      const typing =
        target instanceof HTMLElement &&
        (target.isContentEditable ||
          ["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName));
      if (event.key === "/" && !typing) {
        event.preventDefault();
        searchBox.current?.focus();
      }
    }
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, []);

  const items = documents.data?.items ?? [];
  const pending = documents.data?.items.find(
    (document) => document.id === deleting,
  );

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-col gap-4">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="font-semibold text-2xl tracking-tight">
            Documents
            {documents.isSuccess && (
              <span className="ml-3 font-normal text-lg text-muted-foreground tabular-nums">
                {documents.data.count}
              </span>
            )}
          </h1>
          <Button onClick={(): void => show({ add: add ? undefined : true })}>
            <Plus aria-hidden="true" />
            {add ? "Close" : "Add documents"}
          </Button>
        </div>

        <form
          className="flex flex-wrap gap-2"
          onSubmit={(event: FormEvent<HTMLFormElement>): void => {
            event.preventDefault();
            const form = new FormData(event.currentTarget);
            const query = form.get("q");
            const when = form.get("period");
            show({
              q: optional(typeof query === "string" ? query.trim() : ""),
              period: optional(typeof when === "string" ? when.trim() : ""),
            });
          }}
        >
          <div className="relative min-w-56 flex-1">
            <Search
              aria-hidden="true"
              className="-translate-y-1/2 pointer-events-none absolute top-1/2 left-3 size-4 text-muted-foreground"
            />
            <Input
              ref={searchBox}
              name="q"
              defaultValue={q}
              placeholder="Search in the documents"
              aria-label="Search documents"
              className="h-11 pl-9"
            />
          </div>
          <Input
            name="period"
            defaultValue={period}
            placeholder="Period (1943, 1943-05, 1940/1945)"
            aria-label="Filter by period"
            className="h-11 min-w-48 flex-1"
          />
          <Button type="submit" variant="secondary" className="h-11">
            Apply
          </Button>
        </form>
      </header>

      {add && (
        <UploadForm
          title="Add documents"
          action={actionOf(formats.data)}
          accept={acceptOf(formats.data)}
          onUpload={(files: File[]): void => upload.mutate({ data: { files } })}
          pending={upload.isPending}
          error={upload.isError ? errorMessage(upload.error) : null}
          uploadedCount={upload.isSuccess ? upload.data.length : null}
        />
      )}

      <Notices
        importError={
          importDocument.isError ? errorMessage(importDocument.error) : null
        }
        imported={importDocument.isSuccess ? importDocument.data : null}
        removeError={remove.isError ? errorMessage(remove.error) : null}
      />

      {/* A search answers the question that was asked; the library underneath it would only
          compete with the answer, so it stands aside until the search is cleared. */}
      {q.length > 0 ? (
        <SearchResults
          query={q}
          hits={search.data ?? []}
          pending={search.isPending}
          error={search.isError ? errorMessage(search.error) : null}
          onClear={(): void => show({ q: undefined })}
        />
      ) : (
        <>
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
              {documents.data.count} with information from{" "}
              <span className="font-mono">{period}</span>.{" "}
              <button
                type="button"
                className="underline underline-offset-4"
                onClick={(): void => show({ period: undefined })}
              >
                Show all
              </button>
            </p>
          )}
          {documents.isSuccess &&
            (items.length === 0 ? (
              <EmptyLibrary filtered={period !== ""} />
            ) : (
              <Library
                documents={items}
                onImport={(documentId: string): void =>
                  importDocument.mutate({ data: { document_id: documentId } })
                }
                importingId={
                  importDocument.isPending
                    ? importDocument.variables.data.document_id
                    : null
                }
                onDelete={(documentId: string): void =>
                  show({ delete: documentId })
                }
              />
            ))}
        </>
      )}

      <Dialog
        open={deleting !== undefined}
        onOpenChange={(open: boolean): void => {
          if (!open) show({ delete: undefined });
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete this document?</DialogTitle>
            <DialogDescription>
              {pending ? `"${pending.title}" ` : "The document "}
              and everything read out of it leave the list. Its history is kept,
              so it can be restored.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <DialogClose asChild>
              <Button variant="outline">Cancel</Button>
            </DialogClose>
            <Button
              variant="destructive"
              disabled={remove.isPending}
              onClick={(): void => {
                if (deleting !== undefined)
                  remove.mutate({ documentId: deleting });
              }}
            >
              {remove.isPending ? "Deleting..." : "Delete"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function Notices({
  importError,
  imported,
  removeError,
}: {
  importError: string | null;
  imported: { id: string; name: string; row_count: number } | null;
  removeError: string | null;
}): JSX.Element | null {
  if (!importError && !imported && !removeError) return null;
  return (
    <div className="flex flex-col gap-2 text-sm">
      {importError && (
        <p className="text-destructive">Import failed: {importError}</p>
      )}
      {removeError && (
        <p className="text-destructive">Delete failed: {removeError}</p>
      )}
      {imported && (
        <p className="text-muted-foreground">
          Imported <strong>{imported.name}</strong> —{" "}
          {imported.row_count.toLocaleString()} rows.{" "}
          <Link
            to="/history/$resource/$objectId"
            params={{ resource: "dataset", objectId: imported.id }}
            className="underline underline-offset-4"
          >
            See what it was built from
          </Link>
        </p>
      )}
    </div>
  );
}

/**
 * When a document's information comes from, else when the file arrived. The list sorts and
 * groups by this — a letter written in May belongs under May, not under the day it was
 * scanned — and the row says which of the two it is showing.
 *
 * A date here is a *span*, not a point (`apps/documents/dating.py`), and the bound that
 * decides where it belongs is the **end** of it: a record covering 1998–2004 is a 2004
 * document to someone scrolling a history, not a 1998 one, and grouping by the start scatters
 * long spans back into whatever year they happened to begin. `min` is the fallback for a span
 * that is open at the top ("from 1998 onwards"), where it is the only bound there is.
 */
function timeOf(document: DocumentOut): { at: Date; origin: boolean } {
  const bound = document.date?.max ?? document.date?.min ?? null;
  if (bound !== null) return { at: new Date(bound), origin: true };
  return { at: new Date(document.created), origin: false };
}

type MonthGroup = { key: string; label: string; documents: DocumentOut[] };

function byMonth(documents: DocumentOut[]): MonthGroup[] {
  const sorted = [...documents].sort(
    (a: DocumentOut, b: DocumentOut): number =>
      timeOf(b).at.getTime() - timeOf(a).at.getTime(),
  );
  const groups: MonthGroup[] = [];
  for (const document of sorted) {
    const { at } = timeOf(document);
    const key = `${at.getFullYear()}-${at.getMonth()}`;
    const last = groups.at(-1);
    if (last?.key === key) {
      last.documents.push(document);
      continue;
    }
    groups.push({
      key,
      label: at.toLocaleDateString(undefined, {
        month: "long",
        year: "numeric",
      }),
      documents: [document],
    });
  }
  return groups;
}

type LibraryProps = {
  documents: DocumentOut[];
  onDelete: (id: string) => void;
  onImport: (id: string) => void;
  importingId: string | null;
};

function Library({
  documents,
  onDelete,
  onImport,
  importingId,
}: LibraryProps): JSX.Element {
  return (
    <div className="flex flex-col gap-6">
      {byMonth(documents).map((group) => (
        <section key={group.key} className="flex flex-col gap-1">
          <h2 className="px-1 font-medium text-muted-foreground text-xs uppercase tracking-wider">
            {group.label}
          </h2>
          <ul className="divide-y overflow-hidden rounded-lg border bg-card">
            {group.documents.map((document) => (
              <DocumentRow
                key={document.id}
                document={document}
                onImport={onImport}
                importing={importingId === document.id}
                onDelete={onDelete}
              />
            ))}
          </ul>
        </section>
      ))}
    </div>
  );
}

function DocumentRow({
  document,
  onImport,
  importing,
  onDelete,
}: {
  document: DocumentOut;
  onImport: (id: string) => void;
  importing: boolean;
  onDelete: (id: string) => void;
}): JSX.Element {
  const { at, origin } = timeOf(document);
  return (
    <li className="flex items-center gap-3 pr-2 transition-colors hover:bg-accent/40 has-focus-visible:bg-accent/40">
      <Link
        to="/documents/$documentId"
        params={{ documentId: document.id }}
        search={{}}
        className="flex min-w-0 flex-1 items-center gap-4 py-3 pl-3 outline-none"
      >
        <Thumbnail document={document} />
        <span className="flex min-w-0 flex-col gap-1">
          <span className="truncate font-medium">{document.title}</span>
          <span className="text-muted-foreground text-sm">
            {origin
              ? document.date?.display
              : `Added ${at.toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" })}`}
          </span>
          <span className="flex flex-wrap items-center gap-2">
            <StatusBadge status={document.status} />
            <span className="text-muted-foreground text-xs tabular-nums">
              {document.page_count > 0 &&
                `${document.page_count} page${document.page_count === 1 ? "" : "s"} · `}
              {formatBytes(document.size)}
            </span>
          </span>
        </span>
      </Link>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            aria-label={`Actions for ${document.title}`}
          >
            <MoreVertical aria-hidden="true" />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          <DropdownMenuItem asChild>
            <a href={apiUrl(document.download_url)} download={document.title}>
              <Download aria-hidden="true" />
              Download the original
            </a>
          </DropdownMenuItem>
          <DropdownMenuItem asChild>
            <Link
              to="/history/$resource/$objectId"
              params={{ resource: "document", objectId: document.id }}
            >
              <GitBranch aria-hidden="true" />
              History
            </Link>
          </DropdownMenuItem>
          <DropdownMenuItem
            disabled={importing}
            onSelect={(): void => onImport(document.id)}
          >
            <Table2 aria-hidden="true" />
            {importing ? "Importing..." : "Import as a dataset"}
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem
            variant="destructive"
            onSelect={(): void => onDelete(document.id)}
          >
            <Trash2 aria-hidden="true" />
            Delete
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
    </li>
  );
}

/** Page one, small — the thing a person recognises. A file with no page keeps its glyph. */
export function Thumbnail({
  document,
  className,
}: {
  document: DocumentOut;
  className?: string;
}): JSX.Element {
  return (
    <span
      className={cn(
        "flex h-16 w-12 shrink-0 items-center justify-center overflow-hidden rounded-sm border bg-background",
        className,
      )}
    >
      {document.thumb_url === null ? (
        <FileText aria-hidden="true" className="size-5 text-muted-foreground" />
      ) : (
        <img
          src={apiUrl(document.thumb_url)}
          alt=""
          loading="lazy"
          decoding="async"
          className="h-full w-full object-cover object-top"
        />
      )}
    </span>
  );
}

const STATUS: Record<
  ExtractionStatus,
  { label: string; className: string; busy: boolean } | null
> = {
  // A document that came out fine says so by saying nothing.
  succeeded: null,
  pending: { label: "Preparing", className: "", busy: true },
  running: { label: "Reading", className: "", busy: true },
  partial: {
    label: "Partly read",
    className:
      "border-amber-500/40 bg-amber-500/10 text-amber-900 dark:text-amber-200",
    busy: false,
  },
  failed: {
    label: "Not readable — original kept",
    className: "border-destructive/40 bg-destructive/10 text-destructive",
    busy: false,
  },
};

/**
 * The latest extraction's state, shown only when it is worth a word: a finished run is the
 * normal case and gets no chip. A document nothing extracts (an unknown type) has no run.
 */
export function StatusBadge({
  status,
}: {
  status: ExtractionStatus | null;
}): JSX.Element | null {
  if (status === null) {
    return (
      <Badge variant="outline" className="text-muted-foreground">
        No extractor for this type
      </Badge>
    );
  }
  const shown = STATUS[status];
  if (shown === null) return null;
  return (
    <Badge variant="outline" className={shown.className}>
      {shown.busy && (
        <span className="size-1.5 animate-pulse rounded-full bg-current" />
      )}
      {shown.label}
    </Badge>
  );
}

function EmptyLibrary({ filtered }: { filtered: boolean }): JSX.Element {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed px-6 py-16 text-center">
      <FileText
        aria-hidden="true"
        className="size-8 text-muted-foreground/60"
      />
      {filtered ? (
        <p className="text-muted-foreground">
          No document matches this filter.
        </p>
      ) : (
        <>
          <p className="max-w-prose text-balance">
            This is where your documents live, together with everything that was
            read out of them.
          </p>
          <p className="max-w-prose text-balance text-muted-foreground">
            Add a PDF or a scan — the text is read for you, and the original is
            kept either way.
          </p>
        </>
      )}
    </div>
  );
}

/** The query's words, marked in a snippet the server cut around the hit. */
function marked(text: string, query: string): ReactNode[] {
  const words = query
    .split(/\s+/)
    .filter((word: string): boolean => word.length > 1)
    .map((word: string): string => word.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (words.length === 0) return [text];
  // `split` with one capture group alternates: text, match, text, match... The key is the
  // piece's offset in the snippet, which is what actually identifies it.
  const pieces = text.split(new RegExp(`(${words.join("|")})`, "gi"));
  const nodes: ReactNode[] = [];
  let at = 0;
  for (const [index, piece] of pieces.entries()) {
    nodes.push(
      index % 2 === 1 ? (
        <mark
          key={at}
          className="rounded-xs bg-primary/15 px-0.5 text-foreground"
        >
          {piece}
        </mark>
      ) : (
        piece
      ),
    );
    at += piece.length;
  }
  return nodes;
}

type SearchResultsProps = {
  query: string;
  hits: SearchHitOut[];
  pending: boolean;
  error: string | null;
  onClear: () => void;
};

/**
 * Hits grouped back into the documents they came from: a person searches for a letter, not
 * for an offset, and a card per document with its passages under it is that answer. Each
 * passage links into the document at its own node, so the jump lands on the paper.
 */
function SearchResults({
  query,
  hits,
  pending,
  error,
  onClear,
}: SearchResultsProps): JSX.Element {
  const documents = new Map<string, { title: string; hits: SearchHitOut[] }>();
  for (const hit of hits) {
    const found = documents.get(hit.document_id);
    if (found === undefined) {
      documents.set(hit.document_id, { title: hit.title, hits: [hit] });
    } else {
      found.hits.push(hit);
    }
  }

  return (
    <section className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="font-medium text-muted-foreground text-sm">
          {pending
            ? "Searching..."
            : error
              ? "Search failed"
              : `${documents.size} document${documents.size === 1 ? "" : "s"} for "${query}"`}
        </h2>
        <Button variant="ghost" size="sm" onClick={onClear}>
          Back to all documents
        </Button>
      </div>
      {error && <p className="text-destructive text-sm">{error}</p>}
      {!pending && !error && hits.length === 0 && (
        <p className="rounded-lg border border-dashed px-4 py-8 text-center text-muted-foreground text-sm">
          Nothing found. Try other words, or fewer of them.
        </p>
      )}
      <ul className="flex flex-col gap-3">
        {[...documents].map(([documentId, found]) => (
          <li key={documentId} className="rounded-lg border bg-card p-4">
            <Link
              to="/documents/$documentId"
              params={{ documentId }}
              search={{}}
              className="font-medium underline-offset-4 hover:underline"
            >
              {found.title}
            </Link>
            <ul className="mt-3 flex flex-col gap-2">
              {found.hits.slice(0, 3).map((hit) => (
                <li key={hit.offset} className="flex items-start gap-3 text-sm">
                  {hit.node && hit.node.pages.length > 0 && (
                    <Badge
                      variant="secondary"
                      className="mt-0.5 shrink-0 tabular-nums"
                    >
                      p. {hit.node.pages.join(", ")}
                    </Badge>
                  )}
                  <Link
                    to="/documents/$documentId"
                    params={{ documentId }}
                    search={
                      hit.node
                        ? { nid: hit.node.nid, page: hit.node.pages[0] }
                        : {}
                    }
                    className="min-w-0 text-muted-foreground leading-relaxed hover:text-foreground"
                  >
                    {marked(hit.snippet, query)}
                  </Link>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
    </section>
  );
}
