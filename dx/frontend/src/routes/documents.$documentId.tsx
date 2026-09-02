/**
 * The document workspace: what was read out of the file beside the pages it was read from,
 * and a click in either one lands on the same place in the other.
 *
 * The reading surface is the extracted **document**, not a list of blocks. `content.html` is
 * one semantic tree — sections, headings, lists, tables, the outline over them — and the
 * relations inside it are most of what the extraction is worth, so it is rendered whole, in
 * one flow, at a reading measure. What ties it back to the paper is put in the margin instead
 * of between the paragraphs: every page begins with a marker in the gutter, so a reader always
 * knows which sheet they are on without the text being cut into cards.
 *
 * What makes the jumping possible is in the artifact itself: every structural tag of
 * `content.html` carries `data-nid` (the node) and `data-pages` (where it is drawn), and every
 * region of a page carries the same `nid` with its outline in normalized coordinates. So the
 * text pane needs no parser to select — it finds a node with `querySelector` — and the page
 * pane draws the regions straight onto the rendered page image. The one parse we do keep
 * (`useStructure`) reads the page numbers out of the artifact once, which is what the gutter
 * markers and the "continues on page N" hint are made of.
 *
 * Everything the eye can see is in the URL (`?view=&page=&nid=&rename=`), so a reload, a back
 * button and a shared link all land on the same paragraph of the same page.
 */
import { type Query, useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import {
  ChevronLeft,
  ChevronRight,
  Download,
  ExternalLink,
  GitBranch,
  Info,
  MoreVertical,
  Pencil,
  RefreshCw,
} from "lucide-react";
import {
  type FormEvent,
  type JSX,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  getGetDocumentContentQueryKey,
  getGetDocumentPageQueryKey,
  getGetDocumentQueryKey,
  getListDocumentPagesQueryKey,
  getListExtractionStrategiesQueryKey,
  getListExtractionsQueryKey,
  useGetDocument,
  useGetDocumentContent,
  useGetDocumentPage,
  useListDocumentPages,
  useListExtractionStrategies,
  useListExtractions,
  useReextractDocument,
  useUpdateDocument,
} from "@/api/documents/documents";
import type {
  ContentOut,
  DateOut,
  DocumentOut,
  ExtractionOut,
  ExtractionStatus,
  PageOut,
  PageSummaryOut,
  RegionOut,
  StrategyOut,
  TaskOut,
} from "@/api/model";
import { GetTaskResponse } from "@/api/zod/tasks/tasks";
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
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { apiUrl, errorMessage } from "@/lib/custom-fetch";
import { cn } from "@/lib/utils";
import { StatusBadge } from "./documents.index";

/** Which panes are on screen. `split` shows both from `lg` up, text alone below that. The
 * original file is a link, not a pane: the browser's own viewer does that job better. */
type View = "split" | "text" | "pages";
/** All are optional, so a plain `/documents/<id>` link needs no parameters and the URL only
 * ever carries what the reader actually changed. */
type Search = { view?: View; page?: number; nid?: number; rename?: boolean };

/** The panes fill the window rather than a fixed slice of it: this is a reading surface. */
const PANE = "h-[65svh] min-h-80 lg:h-[calc(100svh-17rem)] lg:min-h-96";

function asView(value: unknown): View {
  switch (value) {
    case "text":
    case "pages":
      return value;
    default:
      return "split";
  }
}

function asPositive(value: unknown): number | undefined {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : undefined;
}

export const Route = createFileRoute("/documents/$documentId")({
  component: DocumentWorkspace,
  // Two panes side by side need more than the shell's reading width (see __root.tsx).
  staticData: { wide: true },
  validateSearch: (search: Record<string, unknown>): Search => ({
    view: "view" in search ? asView(search.view) : undefined,
    page: asPositive(search.page),
    nid: asPositive(search.nid),
    rename:
      search.rename === true || search.rename === "true" ? true : undefined,
  }),
});

/** Polled only while a run is in flight; the stream below is the primary signal. */
const POLL_MS = 2000;

function isActive(status: ExtractionStatus | null | undefined): boolean {
  return status === "pending" || status === "running";
}

/**
 * Follows the run's Celery task over Server-Sent Events: the extraction reports the page it is
 * on (`snapshot.report_progress`), and the stream carries that through without polling. The
 * URL is signed because `EventSource` cannot send the bearer header.
 */
function useRunProgress(
  streamUrl: string | null,
  onFinished: () => void,
): TaskOut | null {
  const [task, setTask] = useState<TaskOut | null>(null);

  useEffect(() => {
    setTask(null);
    if (streamUrl === null) return;
    const source = new EventSource(apiUrl(streamUrl));
    source.addEventListener("status", (event: MessageEvent<string>) => {
      const parsed = GetTaskResponse.safeParse(JSON.parse(event.data));
      if (!parsed.success) return;
      setTask(parsed.data);
      if (parsed.data.ready) {
        // The server closes the stream itself; closing here stops the reconnect.
        source.close();
        onFinished();
      }
    });
    return () => source.close();
  }, [streamUrl, onFinished]);

  return task;
}

type Structure = {
  /** Every node's pages — what "this continues on page N" is read from. */
  pages: Map<number, number[]>;
  /** node → the page it is the first of, i.e. where a page marker belongs in the flow. */
  opens: Map<number, number>;
};

const EMPTY: Structure = { pages: new Map(), opens: new Map() };

/**
 * Where the artifact sits on the paper, read out of the artifact once.
 *
 * `data-pages` is already on every structural tag, so the answer is in the string we were
 * given — no endpoint, no second source of truth. Only leaf nodes open a page: a `<section>`
 * spans its whole chapter and would claim page one for the entire document.
 */
function useStructure(html: string): Structure {
  return useMemo((): Structure => {
    if (html === "") return EMPTY;
    const parsed = new DOMParser().parseFromString(html, "text/html");
    const pages = new Map<number, number[]>();
    for (const node of parsed.querySelectorAll<HTMLElement>(
      "[data-nid][data-pages]",
    )) {
      const on = pagesOf(node);
      if (on.length > 0) pages.set(Number(node.dataset.nid), on);
    }
    const opens = new Map<number, number>();
    const seen = new Set<number>();
    for (const leaf of parsed.querySelectorAll<HTMLElement>(
      "[data-nid][data-pages]:not(:has([data-nid]))",
    )) {
      const first = pagesOf(leaf)[0];
      if (first === undefined || seen.has(first)) continue;
      seen.add(first);
      opens.set(Number(leaf.dataset.nid), first);
    }
    return { pages, opens };
  }, [html]);
}

function pagesOf(node: HTMLElement): number[] {
  return (node.dataset.pages ?? "")
    .split(",")
    .map((value: string): number => Number(value))
    .filter((value: number): boolean => Number.isInteger(value) && value > 0);
}

function DocumentWorkspace(): JSX.Element {
  const { documentId } = Route.useParams();
  const { view = "split", page = 1, nid, rename } = Route.useSearch();
  const navigate = Route.useNavigate();
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
  const pages = useListDocumentPages(documentId, {
    query: { queryKey: getListDocumentPagesQueryKey(documentId) },
  });
  const extractions = useListExtractions(documentId, {
    query: { queryKey: getListExtractionsQueryKey(documentId) },
  });
  const isPdf = document.data?.mime_type === "application/pdf";
  // Only worth asking for where the answer is offered: the strategy picker.
  const strategies = useListExtractionStrategies({
    query: {
      queryKey: getListExtractionStrategiesQueryKey(),
      enabled: document.isSuccess,
    },
  });

  const pageList = pages.data ?? [];
  const current = Math.min(page, Math.max(pageList.length, 1));
  const openPage = useGetDocumentPage(documentId, current, {
    query: {
      queryKey: getGetDocumentPageQueryKey(documentId, current),
      enabled: pageList.length > 0,
    },
  });
  const structure = useStructure(content.data?.html ?? "");

  const refresh = useCallback((): void => {
    for (const queryKey of [
      getGetDocumentQueryKey(documentId),
      getGetDocumentContentQueryKey(documentId),
      getListDocumentPagesQueryKey(documentId),
      getListExtractionsQueryKey(documentId),
    ]) {
      void queryClient.invalidateQueries({ queryKey });
    }
  }, [documentId, queryClient]);

  const running = extractions.data?.find((run) => isActive(run.status)) ?? null;
  const task = useRunProgress(running?.stream_url ?? null, refresh);
  const reextract = useReextractDocument({ mutation: { onSuccess: refresh } });
  const retitle = useUpdateDocument({ mutation: { onSuccess: refresh } });

  /** The one place the workspace's view state changes — and it changes the URL. */
  const show = useCallback(
    (next: Partial<Search>): void => {
      void navigate({
        search: (previous: Search): Search => ({ ...previous, ...next }),
        replace: true,
      });
    },
    [navigate],
  );
  const select = useCallback(
    (selected: number | undefined, onPage?: number): void =>
      show({ nid: selected, page: onPage ?? page }),
    [show, page],
  );
  /** The reader scrolled onto another page: turn the scan, leave the selection alone. */
  const turnTo = useCallback(
    (next: number): void => show({ page: next }),
    [show],
  );

  const hasPages = pageList.length > 0;
  const showText = view === "split" || view === "text";
  const showPages = view === "split" || view === "pages";
  const unreadable = pageList
    .filter((one: PageSummaryOut): boolean => one.failed)
    .map((one: PageSummaryOut): number => one.number);

  if (document.isError) {
    return (
      <p className="text-destructive">
        Failed to load the document: {errorMessage(document.error)}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-4">
      <DocumentHeader
        document={document.data ?? null}
        documentId={documentId}
        strategies={strategies.data ?? []}
        view={view}
        onView={(next: View): void => show({ view: next })}
        hasPages={hasPages}
        onExtract={(strategy: string | undefined, fromRaw: boolean): void =>
          reextract.mutate({
            documentId,
            params: { strategy, from_raw: fromRaw },
          })
        }
        onRename={(): void => show({ rename: true })}
        pending={reextract.isPending}
        error={reextract.isError ? errorMessage(reextract.error) : null}
      />

      {running !== null && <RunBanner run={running} task={task} />}
      {content.data?.extraction?.status === "failed" && (
        <Notice tone="destructive">
          <strong className="font-medium">
            This document could not be read — the original is kept.
          </strong>{" "}
          {content.data.extraction.error} You can still open every page and the
          file itself; "Read again" in the menu tries once more.
        </Notice>
      )}

      <div
        className={cn(
          "grid gap-4",
          // The text is what one reads, so it gets the wider column; the page is a map.
          showText &&
            showPages &&
            "lg:grid-cols-[minmax(0,1.35fr)_minmax(0,1fr)]",
        )}
      >
        {showText && (
          <TextPane
            content={content.data ?? null}
            pending={content.isPending}
            structure={structure}
            unreadable={unreadable}
            nid={nid}
            page={current}
            onSelect={select}
            onReading={turnTo}
          />
        )}

        {showPages && (
          <div
            className={cn(
              "min-w-0",
              !showText && "lg:col-span-2",
              // A phone shows one pane at a time: side by side needs two columns, and
              // stacked they would only bury each other. The Scan tab is one tap away.
              view === "split" && "hidden lg:block",
            )}
          >
            {hasPages ? (
              <ScanPane
                page={openPage.data ?? null}
                pages={pageList}
                selected={nid}
                selectedPages={
                  nid === undefined ? [] : (structure.pages.get(nid) ?? [])
                }
                onSelect={(selected: number): void => select(selected)}
                onPage={(next: number): void => show({ page: next })}
              />
            ) : (
              <EmptyPane
                isPdf={isPdf}
                status={content.data?.status ?? null}
                pending={pages.isPending || document.isPending}
              />
            )}
          </div>
        )}
      </div>

      {/* A phone has no room beside the text, so the scan comes over it: the page the
          paragraph is on, with its box lit. Driven by the same `?nid=` as the split view, so
          a reload lands on the same paragraph with the same page open. */}
      {nid !== undefined && hasPages && (
        <ScanPeek
          page={openPage.data ?? null}
          pages={pageList.length}
          selected={nid}
          onClose={(): void => select(undefined)}
        />
      )}

      <RenameDialog
        open={rename === true}
        title={document.data?.title ?? ""}
        pending={retitle.isPending}
        error={retitle.isError ? errorMessage(retitle.error) : null}
        onClose={(): void => show({ rename: undefined })}
        onSave={(title: string): void => {
          retitle.mutate(
            { documentId, data: { title } },
            { onSuccess: (): void => show({ rename: undefined }) },
          );
        }}
      />
    </div>
  );
}

type HeaderProps = {
  document: DocumentOut | null;
  documentId: string;
  strategies: StrategyOut[];
  view: View;
  onView: (view: View) => void;
  hasPages: boolean;
  onExtract: (strategy: string | undefined, fromRaw: boolean) => void;
  onRename: () => void;
  pending: boolean;
  error: string | null;
};

function DocumentHeader({
  document,
  documentId,
  strategies,
  view,
  onView,
  hasPages,
  onExtract,
  onRename,
  pending,
  error,
}: HeaderProps): JSX.Element {
  const options: { value: View; label: string; shown: boolean }[] = [
    { value: "split", label: "Split", shown: hasPages },
    { value: "text", label: "Text", shown: true },
    { value: "pages", label: "Scan", shown: hasPages },
  ];
  return (
    <header className="flex flex-col gap-3">
      <Link
        to="/documents"
        search={{}}
        className="-ml-2 inline-flex w-fit items-center gap-1 rounded-md px-2 py-1 text-muted-foreground text-sm hover:text-foreground"
      >
        <ChevronLeft aria-hidden="true" className="size-4" />
        Documents
      </Link>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-2">
          <h1 className="break-words font-semibold text-2xl tracking-tight">
            {document?.title ?? "Document"}
          </h1>
          {/* When the document is from, and whether it is readable. Size, type and page count
              are bookkeeping: they are in the list, the scan and the file itself. */}
          {document && (
            <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-muted-foreground text-sm">
              {document.date && <DateLabel date={document.date} />}
              <StatusBadge status={document.status} />
            </div>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <Tabs
            value={view}
            onValueChange={(next: string): void => onView(asView(next))}
          >
            <TabsList>
              {options
                .filter((option) => option.shown)
                .map((option) => (
                  <TabsTrigger key={option.value} value={option.value}>
                    {option.label}
                  </TabsTrigger>
                ))}
            </TabsList>
          </Tabs>
          {document && (
            <>
              <Button variant="outline" size="sm" asChild>
                <a
                  href={apiUrl(document.view_url)}
                  target="_blank"
                  rel="noreferrer"
                  aria-label="Open the original"
                >
                  <ExternalLink aria-hidden="true" />
                  {/* The label is dropped where there is no room; the aria-label is not. */}
                  <span className="hidden sm:inline">Open the original</span>
                </a>
              </Button>
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="ghost" size="icon" aria-label="More actions">
                    <MoreVertical aria-hidden="true" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-64">
                  <DropdownMenuItem onSelect={onRename}>
                    <Pencil aria-hidden="true" />
                    Rename
                  </DropdownMenuItem>
                  <DropdownMenuItem asChild>
                    <a
                      href={apiUrl(document.download_url)}
                      download={document.title}
                    >
                      <Download aria-hidden="true" />
                      Download the original
                    </a>
                  </DropdownMenuItem>
                  <DropdownMenuItem asChild>
                    <Link
                      to="/history/$resource/$objectId"
                      params={{ resource: "document", objectId: documentId }}
                    >
                      <GitBranch aria-hidden="true" />
                      History
                    </Link>
                  </DropdownMenuItem>
                  <DropdownMenuSeparator />
                  <DropdownMenuSub>
                    <DropdownMenuSubTrigger disabled={pending}>
                      <RefreshCw aria-hidden="true" />
                      {pending ? "Queueing..." : "Read again"}
                    </DropdownMenuSubTrigger>
                    <DropdownMenuSubContent className="w-72">
                      <DropdownMenuItem
                        onSelect={(): void => onExtract(undefined, false)}
                      >
                        With the default for this file type
                      </DropdownMenuItem>
                      {strategies.map((strategy) => (
                        <DropdownMenuItem
                          key={strategy.name}
                          title={strategy.description}
                          onSelect={(): void => onExtract(strategy.name, false)}
                        >
                          {strategy.name}
                          <span className="ml-auto text-muted-foreground text-xs tabular-nums">
                            {strategy.tool_version}
                          </span>
                        </DropdownMenuItem>
                      ))}
                      <DropdownMenuSeparator />
                      <DropdownMenuItem
                        title="Rebuild the snapshot from the stored extractor output — no extraction cost"
                        onSelect={(): void => onExtract(undefined, true)}
                      >
                        Rebuild from the stored output
                      </DropdownMenuItem>
                    </DropdownMenuSubContent>
                  </DropdownMenuSub>
                </DropdownMenuContent>
              </DropdownMenu>
            </>
          )}
        </div>
      </div>
      error && (
      <p className="text-destructive text-sm">Extraction refused: {error}</p>)
    </header>
  );
}

function RenameDialog({
  open,
  title,
  pending,
  error,
  onClose,
  onSave,
}: {
  open: boolean;
  title: string;
  pending: boolean;
  error: string | null;
  onClose: () => void;
  onSave: (title: string) => void;
}): JSX.Element {
  return (
    <Dialog
      open={open}
      onOpenChange={(next: boolean): void => {
        if (!next) onClose();
      }}
    >
      <DialogContent>
        <form
          onSubmit={(event: FormEvent<HTMLFormElement>): void => {
            event.preventDefault();
            const value = new FormData(event.currentTarget).get("title");
            if (typeof value === "string" && value.trim() !== "") {
              onSave(value.trim());
            }
          }}
        >
          <DialogHeader>
            <DialogTitle>Rename this document</DialogTitle>
            <DialogDescription>
              The title is yours to choose; nothing that was read out of the
              file changes.
            </DialogDescription>
          </DialogHeader>
          {/* Remounted with the dialog, so the field always opens on the current title. */}
          <Input
            key={String(open)}
            name="title"
            defaultValue={title}
            aria-label="Title"
            className="my-4"
          />
          {error && <p className="mb-2 text-destructive text-sm">{error}</p>}
          <DialogFooter>
            <DialogClose asChild>
              <Button type="button" variant="outline">
                Cancel
              </Button>
            </DialogClose>
            <Button type="submit" disabled={pending}>
              {pending ? "Saving..." : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function Notice({
  tone,
  children,
}: {
  tone: "muted" | "warning" | "destructive";
  children: ReactNode;
}): JSX.Element {
  return (
    <p
      className={cn(
        "flex items-start gap-2 rounded-lg border px-3 py-2 text-sm leading-relaxed",
        tone === "muted" && "bg-muted/50 text-muted-foreground",
        tone === "warning" &&
          "border-amber-500/40 bg-amber-500/10 text-amber-900 dark:text-amber-200",
        tone === "destructive" &&
          "border-destructive/40 bg-destructive/10 text-destructive",
      )}
    >
      <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
      <span className="min-w-0">{children}</span>
    </p>
  );
}

/** What a run is doing right now: the page it reached, straight from the worker. */
function RunBanner({
  run,
  task,
}: {
  run: ExtractionOut;
  task: TaskOut | null;
}): JSX.Element {
  const progress = task?.progress ?? null;
  const percent =
    progress && progress.total > 0
      ? Math.round((100 * progress.current) / progress.total)
      : null;
  return (
    <div className="flex flex-col gap-2 rounded-lg border bg-muted/40 p-3 text-sm">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium">
          {run.status === "pending" ? "Queued" : "Reading"} with {run.extractor}
        </span>
        <span className="text-muted-foreground">
          {progress
            ? `page ${progress.current} of ${progress.total}`
            : task?.state === "STARTED"
              ? "reading the file..."
              : "waiting for a worker..."}
          {" — the pages can be looked at already."}
        </span>
        {percent !== null && (
          <span className="ml-auto tabular-nums">{percent}%</span>
        )}
      </div>
      <div className="h-1.5 overflow-hidden rounded-full bg-muted">
        <div
          className={cn(
            "h-full bg-primary transition-all",
            percent === null && "w-1/3 animate-pulse",
          )}
          style={percent === null ? undefined : { width: `${percent}%` }}
        />
      </div>
    </div>
  );
}

/**
 * The headings of the current snapshot — the artifact's own structure, which is the fastest
 * way through a long document and the part a stack of paragraphs would throw away.
 */

type TextPaneProps = {
  content: ContentOut | null;
  pending: boolean;
  structure: Structure;
  unreadable: number[];
  nid: number | undefined;
  page: number;
  onSelect: (nid: number | undefined, page?: number) => void;
  /** The reader has scrolled a new page's first block past the top of the pane. */
  onReading: (page: number) => void;
};

/** How far down the pane a block has to be before it counts as the one being read. */
const READING_LINE = 0.05;

/**
 * The extracted document, rendered as it is stored and as one flow.
 *
 * Selection and the gutter markers are done on the DOM rather than in React: the artifact is
 * one string, and `data-nid` already identifies every node in it — so the pane sets two data
 * attributes and the stylesheet does the rest (`.document-html` in index.css).
 */
function TextPane({
  content,
  pending,
  structure,
  unreadable,
  nid,
  page,
  onSelect,
  onReading,
}: TextPaneProps): JSX.Element {
  const container = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const shown = useRef<{ nid: number | undefined; page: number }>({
    nid,
    page,
  });
  const html = content?.html ?? "";

  // After every render, not on a dependency change: the marks live in HTML that React owns
  // as one opaque string, so any re-render of the pane (a refetch returning the same text)
  // drops them. Re-applying them is a handful of `querySelector`s; scrolling stays
  // conditional below.
  useEffect(() => {
    const root = container.current;
    if (!root) return;
    for (const marked of root.querySelectorAll(
      "[data-selected], [data-marker]",
    )) {
      marked.removeAttribute("data-selected");
      marked.removeAttribute("data-marker");
      marked.removeAttribute("data-marker-rule");
    }
    // Where each page begins, in the margin — the paper's rhythm without cutting the text up.
    // Every page but the first also gets the hairline: a document does not open on a break.
    let firstPage = true;
    for (const [openerNid, opened] of structure.opens) {
      const opener = root.querySelector(`[data-nid="${openerNid}"]`);
      if (!(opener instanceof HTMLElement)) continue;
      opener.dataset.marker = `Page ${opened}`;
      if (!firstPage) opener.dataset.markerRule = "true";
      firstPage = false;
    }
    const target =
      nid === undefined ? null : root.querySelector(`[data-nid="${nid}"]`);
    if (target instanceof HTMLElement) target.dataset.selected = "true";

    const nidChanged = shown.current.nid !== nid;
    const pageChanged = shown.current.page !== page;
    shown.current = { nid, page };
    if (nidChanged && target instanceof HTMLElement) {
      target.scrollIntoView({ block: "center", behavior: "smooth" });
    } else if (pageChanged && !nidChanged) {
      // Turning a page in the other pane brings the text along: the first node drawn there.
      const first = [
        ...root.querySelectorAll<HTMLElement>("[data-pages]"),
      ].find((node) =>
        (node.dataset.pages ?? "").split(",").includes(String(page)),
      );
      first?.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  });

  // The listener goes on the container itself rather than through an `onClick` prop: the
  // elements it reacts to are the artifact's, not React's, and a reading surface is not a
  // control. Every node is also reachable with the keyboard from the outline, the timeline
  // and the page regions, which are ordinary buttons.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `html` is the trigger — the pane does not exist until the artifact arrives, and this is what attaches the listener to it
  useEffect(() => {
    const root = container.current;
    if (!root) return;
    function handle(event: Event): void {
      const clicked = event.target;
      if (!(clicked instanceof Element)) return;
      const node = clicked.closest("[data-nid]");
      if (!(node instanceof HTMLElement)) return;
      onSelect(Number(node.dataset.nid), pagesOf(node)[0]);
    }
    root.addEventListener("click", handle);
    return () => root.removeEventListener("click", handle);
  }, [onSelect, html]);

  // Scrolling the text turns the scan beside it: whichever page's blocks have crossed the
  // reading line is the page on the right. `shown` is updated first, so the effect above sees
  // no change and does not scroll the text back — the reader leads, the pane follows.
  // biome-ignore lint/correctness/useExhaustiveDependencies: `html` is the trigger — the surface does not exist until the artifact arrives
  useEffect(() => {
    const box = scroller.current;
    const root = container.current;
    if (!box || !root) return;
    let queued = false;
    function follow(): void {
      queued = false;
      const surface = scroller.current;
      const rendered = container.current;
      if (!surface || !rendered) return;
      const frame = surface.getBoundingClientRect();
      const line = frame.top + frame.height * READING_LINE;
      let reading: number | undefined;
      for (const node of rendered.querySelectorAll<HTMLElement>(
        "[data-pages]",
      )) {
        if (node.getBoundingClientRect().top > line) break;
        reading = pagesOf(node)[0] ?? reading;
      }
      if (reading !== undefined && reading !== shown.current.page) {
        shown.current.page = reading;
        onReading(reading);
      }
    }
    function schedule(): void {
      if (queued) return;
      queued = true;
      requestAnimationFrame(follow);
    }
    // The pane scrolls from `lg` up; below that the window does.
    box.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("scroll", schedule, { passive: true });
    return () => {
      box.removeEventListener("scroll", schedule);
      window.removeEventListener("scroll", schedule);
    };
  }, [onReading, html]);

  if (pending) {
    return <PaneBox>Loading the extracted text...</PaneBox>;
  }
  if (html === "") {
    return (
      <PaneBox>
        {content?.status === null
          ? "No extractor handles this kind of file, so there is nothing to read here."
          : isActive(content?.status)
            ? "The extraction is running; this pane fills itself when it lands."
            : "The extraction produced no content."}
      </PaneBox>
    );
  }
  return (
    // On a phone the document scrolls with the page — a reading surface inside its own
    // scroller is a box to fight with. From `lg` up it becomes a pane again, beside the scan.
    <div
      className={cn(
        "flex min-w-0 flex-col gap-2",
        "lg:h-[calc(100svh-17rem)] lg:min-h-96",
      )}
    >
      {unreadable.length > 0 && (
        <Notice tone="muted">
          {unreadable.length === 1
            ? `Page ${unreadable[0]} could not be read`
            : `Pages ${unreadable.join(", ")} could not be read`}{" "}
          — they are missing here, but you can still look at them in the scan.
        </Notice>
      )}
      {/* The scroller and the page are separate boxes: the measure, the gutter and the
          rhythm belong to the document (`.document-html` in index.css), not to the pane. */}
      <div
        ref={scroller}
        className="rounded-lg border bg-card lg:min-h-0 lg:flex-1 lg:overflow-auto"
      >
        <div
          ref={container}
          className="document-html"
          // biome-ignore lint/security/noDangerouslySetInnerHtml: sanitized server-side with nh3 against the tag allowlist that is the node vocabulary (apps/documents/snapshot.py)
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </div>
    </div>
  );
}

/** The scan over the text, for a screen too narrow to hold both (`lg:hidden`). */
function ScanPeek({
  page,
  pages,
  selected,
  onClose,
}: {
  page: PageOut | null;
  pages: number;
  selected: number;
  onClose: () => void;
}): JSX.Element {
  useEffect(() => {
    function escape(event: KeyboardEvent): void {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [onClose]);

  const region = page?.regions.find(
    (one: RegionOut): boolean => one.nid === selected,
  );
  const ratio =
    page?.width && page?.height
      ? `${page.width} / ${page.height}`
      : "1 / 1.414";
  return (
    <div className="fixed inset-0 z-50 flex flex-col justify-end bg-foreground/60 lg:hidden">
      <button
        type="button"
        aria-label="Close"
        className="flex-1"
        onClick={onClose}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Page ${page?.number ?? ""} of the scan`}
        className="flex max-h-[85svh] flex-col gap-2 rounded-t-xl border-t bg-background p-3 pb-safe"
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate text-muted-foreground text-sm">
            {region ? region.text.slice(0, 60) : `Page ${page?.number ?? ""}`}
          </span>
          <span className="shrink-0 text-muted-foreground text-xs tabular-nums">
            Page {page?.number ?? "?"} of {pages}
          </span>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Close
          </Button>
        </div>
        {page === null ? (
          <PaneBox>Loading the page...</PaneBox>
        ) : (
          <div className="flex min-h-0 flex-1 justify-center overflow-auto">
            <div
              className="relative h-fit max-h-full max-w-full"
              style={{ aspectRatio: ratio }}
            >
              <img
                src={apiUrl(page.image_url)}
                alt={`Page ${page.number}`}
                className="block h-full w-full rounded-sm bg-background object-contain"
                decoding="async"
              />
              {located(page.regions).map((one) => (
                <span
                  key={`${one.nid}-${one.order}`}
                  className="page-region pointer-events-none"
                  data-selected={one.nid === selected ? "true" : undefined}
                  style={{
                    left: `${one.x0 * 100}%`,
                    top: `${one.y0 * 100}%`,
                    width: `${(one.x1 - one.x0) * 100}%`,
                    height: `${(one.y1 - one.y0) * 100}%`,
                    clipPath: clipOf(one),
                  }}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

function PaneBox({ children }: { children: ReactNode }): JSX.Element {
  return (
    <div
      className={cn(
        "flex items-center justify-center rounded-lg border p-4 text-center text-muted-foreground text-sm",
        PANE,
      )}
    >
      {children}
    </div>
  );
}

function EmptyPane({
  isPdf,
  status,
  pending,
}: {
  isPdf: boolean;
  status: ExtractionStatus | null;
  pending: boolean;
}): JSX.Element {
  return (
    <PaneBox>
      {pending
        ? "Loading the pages..."
        : isActive(status)
          ? "The pages appear once the extraction lands."
          : isPdf
            ? "This snapshot has no pages."
            : "Only scanned or paged documents have page images."}
    </PaneBox>
  );
}

type Box = { x0: number; y0: number; x1: number; y1: number };
/** A region can say which page a node is on without saying where — nothing to draw then. */
type LocatedRegion = RegionOut & Box;

/** The region's envelope: the stored one, else the box around the polygon it did store. */
function envelope(region: RegionOut): Box | null {
  const { x0, y0, x1, y1, polygon } = region;
  if (x0 !== null && y0 !== null && x1 !== null && y1 !== null) {
    return { x0, y0, x1, y1 };
  }
  if (polygon === null || polygon.length === 0) return null;
  const xs = polygon.map((point: number[]): number => point[0] ?? 0);
  const ys = polygon.map((point: number[]): number => point[1] ?? 0);
  return {
    x0: Math.min(...xs),
    y0: Math.min(...ys),
    x1: Math.max(...xs),
    y1: Math.max(...ys),
  };
}

function located(regions: RegionOut[]): LocatedRegion[] {
  return regions.flatMap((region: RegionOut): LocatedRegion[] => {
    const box = envelope(region);
    return box === null ? [] : [{ ...region, ...box }];
  });
}

/**
 * The polygon as a clip path over the region's own box, so a block that is not a rectangle —
 * a wrapped column, a slanted scan — is outlined as it really sits. `clip-path` also clips
 * hit-testing, so the button keeps the shape it shows.
 */
function clipOf(region: LocatedRegion): string | undefined {
  const width = region.x1 - region.x0;
  const height = region.y1 - region.y0;
  if (region.polygon === null || region.polygon.length < 3) return undefined;
  if (width <= 0 || height <= 0) return undefined;
  const points = region.polygon.map((point: number[]): string => {
    const [x = 0, y = 0] = point;
    return `${(((x - region.x0) / width) * 100).toFixed(2)}% ${(((y - region.y0) / height) * 100).toFixed(2)}%`;
  });
  return `polygon(${points.join(",")})`;
}

type ScanPaneProps = {
  page: PageOut | null;
  pages: PageSummaryOut[];
  selected: number | undefined;
  selectedPages: number[];
  onSelect: (nid: number) => void;
  onPage: (page: number) => void;
};

/**
 * The page as it was scanned, with one outline per region. The coordinates are normalized to
 * [0, 1] at extraction, so they are laid out in percentages and fit whatever width the pane
 * happens to have.
 */
function ScanPane({
  page,
  pages,
  selected,
  selectedPages,
  onSelect,
  onPage,
}: ScanPaneProps): JSX.Element {
  if (page === null) return <PaneBox>Loading the page...</PaneBox>;
  // The frame keeps the page's own proportions, so the regions — which are fractions of the
  // page, not pixels — land where they belong at whatever size the pane happens to be.
  const ratio =
    page.width && page.height ? `${page.width} / ${page.height}` : "1 / 1.414";
  const continues = selectedPages.find(
    (number: number): boolean => number > page.number,
  );
  return (
    <div
      className={cn(
        "flex flex-col gap-2 rounded-lg border bg-muted/30 p-2",
        PANE,
      )}
    >
      <div className="relative flex min-h-0 flex-1 justify-center">
        <div
          className="relative max-h-full max-w-full"
          style={{ aspectRatio: ratio }}
        >
          <img
            src={apiUrl(page.image_url)}
            alt={`Page ${page.number} of ${pages.length}`}
            className="block h-full w-full rounded-sm bg-background object-contain shadow-sm"
            decoding="async"
          />
          {located(page.regions).map((region) => (
            <button
              key={`${region.nid}-${region.order}`}
              type="button"
              title={`<${region.tag}> #${region.nid}\n${region.text.slice(0, 200)}`}
              onClick={(): void => onSelect(region.nid)}
              data-selected={region.nid === selected ? "true" : undefined}
              className="page-region"
              style={{
                left: `${region.x0 * 100}%`,
                top: `${region.y0 * 100}%`,
                width: `${(region.x1 - region.x0) * 100}%`,
                height: `${(region.y1 - region.y0) * 100}%`,
                clipPath: clipOf(region),
              }}
            />
          ))}
        </div>

        {/* Over the page, where a reader's eye already is: which sheet this is, and whether
            the passage they picked runs on. */}
        <div className="pointer-events-none absolute inset-x-0 bottom-1 flex flex-wrap items-center justify-center gap-2">
          {continues !== undefined && (
            <span className="rounded-full bg-foreground/85 px-3 py-1 font-medium text-background text-xs">
              continues on page {continues}
            </span>
          )}
          <span className="rounded-full bg-foreground/85 px-3 py-1 font-medium text-background text-xs tabular-nums">
            Page {page.number} of {pages.length}
          </span>
        </div>
      </div>

      <div className="flex items-center justify-between gap-2 px-1 text-muted-foreground text-xs">
        <span className={cn(page.failed && "text-destructive")}>
          {page.failed
            ? "This page could not be read"
            : page.regions.length === 0
              ? "Nothing was found on this page"
              : `${page.regions.length} ${page.regions.length === 1 ? "block" : "blocks"}`}
          {page.label &&
            page.label !== String(page.number) &&
            ` · label ${page.label}`}
        </span>
        <span className="flex items-center gap-1">
          {page.date && <DateLabel date={page.date} />}
          <Button
            variant="ghost"
            size="icon"
            aria-label="Previous page"
            disabled={page.number <= 1}
            onClick={(): void => onPage(page.number - 1)}
          >
            <ChevronLeft aria-hidden="true" />
          </Button>
          <Button
            variant="ghost"
            size="icon"
            aria-label="Next page"
            disabled={page.number >= pages.length}
            onClick={(): void => onPage(page.number + 1)}
          >
            <ChevronRight aria-hidden="true" />
          </Button>
        </span>
      </div>

      {pages.length > 1 && (
        <ol className="flex shrink-0 gap-2 overflow-x-auto pb-1">
          {pages.map((thumb) => (
            <li key={thumb.number}>
              <button
                type="button"
                onClick={(): void => onPage(thumb.number)}
                className={cn(
                  "flex w-14 shrink-0 flex-col items-center gap-1 rounded border-2 p-0.5 text-xs",
                  thumb.number === page.number
                    ? "border-primary"
                    : "border-transparent hover:border-border",
                )}
              >
                <img
                  src={apiUrl(thumb.thumb_url)}
                  alt=""
                  loading="lazy"
                  decoding="async"
                  className="w-full rounded-xs bg-background"
                />
                <span
                  className={cn(
                    "tabular-nums",
                    thumb.failed && "text-destructive",
                    !thumb.failed &&
                      thumb.region_count === 0 &&
                      "text-muted-foreground/50",
                  )}
                  title={
                    thumb.failed
                      ? "could not be read"
                      : thumb.region_count === 0
                        ? "nothing found on this page"
                        : undefined
                  }
                >
                  {thumb.number}
                </span>
              </button>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** The original file in the browser's own PDF viewer — the bytes, not our reading of them. */
/** `2026-08-05` → `05.08.2026`; a month or a year keeps the precision it has. */
function plainDate(iso: string): string {
  const [year, month, day] = iso.split("-");
  if (day) return `${day}.${month}.${year}`;
  return month ? `${month}.${year}` : (year ?? iso);
}

/** What a reader wants from a date: the date. How it was arrived at is in the tooltip. */
function dateText(date: DateOut): string {
  if (date.min && date.max) {
    return date.min === date.max
      ? plainDate(date.min)
      : `${plainDate(date.min)} - ${plainDate(date.max)}`;
  }
  if (date.max) return `until ${plainDate(date.max)}`;
  if (date.min) return `from ${plainDate(date.min)}`;
  return date.edtf;
}

function DateLabel({ date }: { date: DateOut }): JSX.Element {
  return (
    <span
      title={`EDTF ${date.edtf} · ${date.source}${date.conf === null ? "" : ` · ${date.conf.toFixed(2)}`}`}
    >
      {dateText(date)}
    </span>
  );
}
