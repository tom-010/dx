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
  Eye,
  EyeOff,
  GitBranch,
  Info,
  Maximize2,
  MoreVertical,
  Pencil,
  RefreshCw,
  X,
} from "lucide-react";
import {
  type FormEvent,
  type JSX,
  type ReactNode,
  type PointerEvent as ReactPointerEvent,
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
type Search = {
  view?: View;
  page?: number;
  nid?: number;
  rename?: boolean;
  /** Show the standing matter the reader was asked about — off unless the URL says so. */
  aside?: boolean;
  /** The page on its own, over everything else. A modal is view state: it belongs in the URL,
   * so a shared link opens on the same page, blown up. */
  full?: boolean;
};

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
    aside: search.aside === true || search.aside === "true" ? true : undefined,
    full: search.full === true || search.full === "true" ? true : undefined,
  }),
});

/** Polled only while a run is in flight, and the stream below is the primary signal — so this
 * is the fallback's fallback and can afford to be slow. */
const POLL_MS = 4000;

function isActive(status: ExtractionStatus | null | undefined): boolean {
  return status === "pending" || status === "running";
}

/**
 * Follows the run's Celery task over Server-Sent Events: the extraction reports the page it is
 * on (`snapshot.report_progress`), and the stream carries that through without polling. The
 * URL is signed because `EventSource` cannot send the bearer header.
 *
 * **The run's id is what identifies the stream, not its URL.** The signature carries a
 * timestamp (`tasks.sign_stream`), so every refetch of the run — every two seconds while one
 * is going — hands back a different `stream_url` for the same task. Keyed on the URL, this
 * effect tore down the EventSource and opened another one twice a second, and every abandoned
 * one left a request thread on the server holding a database connection until its next
 * heartbeat write hit the closed socket. One run, one stream.
 */
function useRunProgress(
  runId: string | null,
  streamUrl: string | null,
  onFinished: () => void,
): TaskOut | null {
  const [task, setTask] = useState<TaskOut | null>(null);
  // Read at connect time, never depended on: it is a fresh signature of the same thing.
  const link = useRef<string | null>(streamUrl);
  link.current = streamUrl;

  useEffect(() => {
    setTask(null);
    const url = link.current;
    if (runId === null || url === null) return;
    const source = new EventSource(apiUrl(url));
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
  }, [runId, onFinished]);

  return task;
}

type Structure = {
  /** Every node's pages — what "this continues on page N" is read from. */
  pages: Map<number, number[]>;
  /** node → the page it is the first of, i.e. where a page marker belongs in the flow. */
  opens: Map<number, number>;
  /** How many nodes the reading marked as standing matter — none means no toggle to offer. */
  aside: number;
};

const EMPTY: Structure = { pages: new Map(), opens: new Map(), aside: 0 };

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
    const aside = parsed.querySelectorAll("[data-aside]").length;
    return { pages, opens, aside };
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
  const {
    view = "split",
    page = 1,
    nid,
    rename,
    aside,
    full,
  } = Route.useSearch();
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
  /** Whether a run is really in flight. A run whose worker is gone still says "running", but
      nothing is going to land, so no pane should promise that it will. */
  const inFlight = running !== null && !running.stale;
  const task = useRunProgress(
    running?.id ?? null,
    running?.stream_url ?? null,
    refresh,
  );
  const reextract = useReextractDocument({ mutation: { onSuccess: refresh } });
  /** Read the document again with the default strategy — resuming whatever a dead or failed
      run already got through. */
  const retry = useCallback(
    (): void => reextract.mutate({ documentId, params: { from_raw: false } }),
    [reextract, documentId],
  );
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
        asideCount={structure.aside}
        aside={aside === true}
        onAside={(next: boolean): void =>
          show({ aside: next ? true : undefined })
        }
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

      {running !== null && (
        <RunBanner
          run={running}
          task={task}
          resumable={content.data?.resumable_pages ?? 0}
          pending={reextract.isPending}
          onRetry={retry}
        />
      )}
      {content.data?.extraction?.status === "failed" && (
        <FailedBanner
          error={content.data.extraction.error}
          resumable={content.data.resumable_pages}
          pending={reextract.isPending}
          onRetry={retry}
        />
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
            running={inFlight}
            pending={content.isPending}
            structure={structure}
            unreadable={unreadable}
            nid={nid}
            page={current}
            aside={aside === true}
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
                onOpen={(): void => show({ full: true })}
              />
            ) : (
              <EmptyPane
                isPdf={isPdf}
                noExtractor={content.data?.status === null}
                running={inFlight}
                pending={pages.isPending || document.isPending}
              />
            )}
          </div>
        )}
      </div>

      {full === true && hasPages && (
        <PageLightbox
          page={openPage.data ?? null}
          pages={pageList.length}
          selected={nid}
          onSelect={(selected: number): void => select(selected)}
          onPage={(next: number): void => show({ page: next })}
          onClose={(): void => show({ full: undefined })}
        />
      )}

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
  /** Standing matter: how much of it there is, whether it is shown, and the switch. */
  asideCount: number;
  aside: boolean;
  onAside: (aside: boolean) => void;
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
  asideCount,
  aside,
  onAside,
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
          {/* Every letter carries the same letterhead, address and bank line. They are read
              and kept, but they are not what the document says, so they start folded away —
              and the switch lives up here rather than costing the reader a line of page. */}
          {asideCount > 0 && (
            <Button
              variant="ghost"
              size="sm"
              aria-pressed={aside}
              title={`${aside ? "Hide" : "Show"} the letterhead, address and contact details (${asideCount})`}
              aria-label={`${aside ? "Hide" : "Show"} the letterhead, address and contact details`}
              className="text-muted-foreground tabular-nums"
              onClick={(): void => onAside(!aside)}
            >
              {aside ? (
                <EyeOff aria-hidden="true" />
              ) : (
                <Eye aria-hidden="true" />
              )}
              {asideCount}
            </Button>
          )}
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
      {error && (
        <p className="text-destructive text-sm">Extraction refused: {error}</p>
      )}
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

/**
 * A run that gave up. The button is the point: reading is resumable — every page an earlier
 * run got back is stored (`DraftPage`), so trying again pays only for what is still missing,
 * which is what makes it worth offering next to the error rather than buried in a menu.
 */
function FailedBanner({
  error,
  resumable,
  pending,
  onRetry,
}: {
  error: string;
  resumable: number;
  pending: boolean;
  onRetry: () => void;
}): JSX.Element {
  return (
    <Notice tone="destructive">
      <strong className="font-medium">
        This document could not be read — the original is kept.
      </strong>{" "}
      {error} You can still open every page and the file itself.{" "}
      {resumable > 0 && `${keptPages(resumable)} `}
      <RetryButton pending={pending} onRetry={onRetry} />
    </Notice>
  );
}

/** What a retry will not have to pay for again — the run's state, in one line. */
function keptPages(resumable: number): string {
  return `${resumable} ${resumable === 1 ? "page is" : "pages are"} already read and will not be read again.`;
}

function RetryButton({
  pending,
  onRetry,
}: {
  pending: boolean;
  onRetry: () => void;
}): JSX.Element {
  return (
    <Button
      variant="outline"
      size="sm"
      className="mt-2 ml-1 align-middle"
      disabled={pending}
      onClick={onRetry}
    >
      <RefreshCw aria-hidden="true" />
      {pending ? "Queueing..." : "Read again"}
    </Button>
  );
}

/** What a run is doing right now: the page it reached, straight from the worker. */
function RunBanner({
  run,
  task,
  resumable,
  pending,
  onRetry,
}: {
  run: ExtractionOut;
  task: TaskOut | null;
  resumable: number;
  pending: boolean;
  onRetry: () => void;
}): JSX.Element {
  const progress = task?.progress ?? null;
  const percent =
    progress && progress.total > 0
      ? Math.round((100 * progress.current) / progress.total)
      : null;
  // A run whose worker was restarted or killed keeps saying "reading" for ever. Say what has
  // actually happened and offer the way out, which picks up whatever that run got through.
  if (run.stale) {
    return (
      <Notice tone="warning">
        <strong className="font-medium">
          This reading stopped — its worker is gone.
        </strong>{" "}
        Nothing has come back from it for a while.{" "}
        {resumable > 0 && `${keptPages(resumable)} `}
        <RetryButton pending={pending} onRetry={onRetry} />
      </Notice>
    );
  }
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
  /** Whether a run is really in flight — see `inFlight` in the workspace. */
  running: boolean;
  pending: boolean;
  structure: Structure;
  unreadable: number[];
  nid: number | undefined;
  page: number;
  /** Whether the standing matter is on screen. */
  aside: boolean;
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
  running,
  pending,
  structure,
  unreadable,
  nid,
  page,
  aside,
  onSelect,
  onReading,
}: TextPaneProps): JSX.Element {
  const container = useRef<HTMLDivElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  /** The page this pane itself reported while the reader scrolled — not one to scroll to. */
  const reported = useRef<number | undefined>(undefined);
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
    // When each passage is from, in the margin under the pointer: the artifact carries the
    // estimate as EDTF on the tag itself, and a reader wants a date, not a notation.
    for (const dated of root.querySelectorAll<HTMLElement>("[data-date]")) {
      dated.dataset.when = edtfText(dated.dataset.date ?? "");
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
    } else if (pageChanged && !nidChanged && page !== reported.current) {
      // Turning a page in the other pane brings the text along: the first node drawn there.
      const first = [
        ...root.querySelectorAll<HTMLElement>("[data-pages]"),
      ].find(
        (node) =>
          // Standing matter that is folded away cannot be scrolled to.
          node.getClientRects().length > 0 &&
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
        const rect = node.getBoundingClientRect();
        if (rect.height === 0) continue; // folded away: it is not what is being read
        if (rect.top > line) break;
        reading = pagesOf(node)[0] ?? reading;
      }
      if (
        reading !== undefined &&
        reading !== reported.current &&
        reading !== shown.current.page
      ) {
        reported.current = reading;
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
          : running
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
          data-aside-shown={aside ? "true" : undefined}
          // biome-ignore lint/security/noDangerouslySetInnerHtml: sanitized server-side with nh3 against the tag allowlist that is the node vocabulary (apps/documents/snapshot.py)
          dangerouslySetInnerHTML={{ __html: html }}
        />
      </div>
    </div>
  );
}

/**
 * One page, as big as the window will make it: the same image and the same regions as the
 * pane, with the neighbours a click away. Escape and the arrow keys work, because a reader
 * looking closely at page 4 wants page 5 without going back to the pane for it.
 */
function PageLightbox({
  page,
  pages,
  selected,
  onSelect,
  onPage,
  onClose,
}: {
  page: PageOut | null;
  pages: number;
  selected: number | undefined;
  onSelect: (nid: number) => void;
  onPage: (page: number) => void;
  onClose: () => void;
}): JSX.Element {
  const number = page?.number ?? 1;
  useEffect(() => {
    function onKey(event: KeyboardEvent): void {
      if (event.key === "Escape") onClose();
      if (event.key === "ArrowRight" && number < pages) onPage(number + 1);
      if (event.key === "ArrowLeft" && number > 1) onPage(number - 1);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, onPage, number, pages]);

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={`Page ${number} of ${pages}`}
      className="fixed inset-0 z-50 flex flex-col gap-2 bg-background/95 p-3 backdrop-blur-sm"
    >
      <div className="flex shrink-0 items-center justify-between gap-2">
        <span className="text-muted-foreground text-sm tabular-nums">
          Page {number} of {pages}
          <span className="ml-2 hidden sm:inline">
            — scroll to zoom, drag to move
          </span>
        </span>
        <Button variant="ghost" size="sm" onClick={onClose}>
          <X aria-hidden="true" />
          Close
        </Button>
      </div>
      <div className="flex min-h-0 flex-1 items-center justify-center gap-2">
        <Button
          variant="outline"
          size="icon"
          aria-label="Previous page"
          disabled={number <= 1}
          onClick={(): void => onPage(number - 1)}
        >
          <ChevronLeft aria-hidden="true" />
        </Button>
        {page === null ? (
          <PaneBox>Loading the page...</PaneBox>
        ) : (
          <PageImage
            page={page}
            selected={selected}
            onSelect={onSelect}
            ratio={ratioOf(page)}
            className="max-h-full max-w-full"
          />
        )}
        <Button
          variant="outline"
          size="icon"
          aria-label="Next page"
          disabled={number >= pages}
          onClick={(): void => onPage(number + 1)}
        >
          <ChevronRight aria-hidden="true" />
        </Button>
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
    function onKey(event: KeyboardEvent): void {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
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
  noExtractor,
  running,
  pending,
}: {
  isPdf: boolean;
  noExtractor: boolean;
  running: boolean;
  pending: boolean;
}): JSX.Element {
  return (
    <PaneBox>
      {pending
        ? "Loading the pages..."
        : running
          ? "The pages appear once the extraction lands."
          : noExtractor
            ? "No extractor handles this kind of file."
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

/** How far the wheel may magnify a page, and how much of a notch one turn is worth. */
const MAX_ZOOM = 8;
const WHEEL_ZOOM = 400;

/** The page's own transform: a scale and the offset of its top-left corner inside the frame. */
type Zoom = { scale: number; x: number; y: number };
const UNZOOMED: Zoom = { scale: 1, x: 0, y: 0 };

function clamp(value: number, low: number, high: number): number {
  return Math.min(Math.max(value, low), high);
}

/** No blank margin beside a magnified page: the frame stays covered by it. */
function settle(next: Zoom, width: number, height: number): Zoom {
  return {
    scale: next.scale,
    x: clamp(next.x, width * (1 - next.scale), 0),
    y: clamp(next.y, height * (1 - next.scale), 0),
  };
}

/**
 * One page image with its regions on it, magnified by the wheel and dragged with the pointer.
 *
 * The regions are children of the transformed box rather than of the frame, so they are scaled
 * by the same matrix as the pixels and cannot drift away from the words they outline. Zoom is
 * deliberately *not* in the URL: it is where a reader's eye is, like a scroll position, and it
 * changes with every notch of the wheel.
 */
function PageImage({
  page,
  selected,
  onSelect,
  ratio,
  className,
}: {
  page: PageOut;
  selected: number | undefined;
  onSelect: ((nid: number) => void) | null;
  ratio: string;
  className?: string;
}): JSX.Element {
  const frame = useRef<HTMLDivElement>(null);
  const [zoom, setZoom] = useState<Zoom>(UNZOOMED);
  const drag = useRef<{ x: number; y: number } | null>(null);

  // Back to the whole page whenever another one is put in the frame.
  // biome-ignore lint/correctness/useExhaustiveDependencies: the page number is the identity of what is shown
  useEffect(() => setZoom(UNZOOMED), [page.number]);

  // A non-passive listener, because a wheel over the page has to zoom it instead of scrolling
  // the pane behind it — React's own onWheel cannot call preventDefault.
  useEffect(() => {
    const box = frame.current;
    if (!box) return;
    function onWheel(event: WheelEvent): void {
      if (!box) return;
      event.preventDefault();
      const rect = box.getBoundingClientRect();
      const px = event.clientX - rect.left;
      const py = event.clientY - rect.top;
      setZoom((now: Zoom): Zoom => {
        const scale = clamp(
          now.scale * Math.exp(-event.deltaY / WHEEL_ZOOM),
          1,
          MAX_ZOOM,
        );
        if (scale === 1) return UNZOOMED;
        // Whatever sits under the pointer stays under the pointer.
        const grew = scale / now.scale;
        return settle(
          { scale, x: px - grew * (px - now.x), y: py - grew * (py - now.y) },
          rect.width,
          rect.height,
        );
      });
    }
    box.addEventListener("wheel", onWheel, { passive: false });
    return () => box.removeEventListener("wheel", onWheel);
  }, []);

  const zoomed = zoom.scale > 1;
  return (
    <div
      ref={frame}
      className={cn(
        "relative overflow-hidden",
        zoomed && "cursor-grab",
        className,
      )}
      style={{ aspectRatio: ratio, touchAction: zoomed ? "none" : undefined }}
      onPointerDown={(event: ReactPointerEvent<HTMLDivElement>): void => {
        if (!zoomed) return;
        drag.current = { x: event.clientX - zoom.x, y: event.clientY - zoom.y };
        event.currentTarget.setPointerCapture(event.pointerId);
      }}
      onPointerMove={(event: ReactPointerEvent<HTMLDivElement>): void => {
        const from = drag.current;
        const box = frame.current;
        if (from === null || box === null) return;
        const rect = box.getBoundingClientRect();
        setZoom(
          (now: Zoom): Zoom =>
            settle(
              {
                scale: now.scale,
                x: event.clientX - from.x,
                y: event.clientY - from.y,
              },
              rect.width,
              rect.height,
            ),
        );
      }}
      onPointerUp={(): void => {
        drag.current = null;
      }}
    >
      <div
        className="absolute inset-0 origin-top-left"
        style={{
          transform: `translate(${zoom.x}px, ${zoom.y}px) scale(${zoom.scale})`,
        }}
      >
        <img
          src={apiUrl(page.image_url)}
          alt={`Page ${page.number}`}
          className="block h-full w-full rounded-sm bg-background object-contain shadow-sm"
          decoding="async"
          draggable={false}
        />
        {located(page.regions).map((region) => {
          const box = {
            left: `${region.x0 * 100}%`,
            top: `${region.y0 * 100}%`,
            width: `${(region.x1 - region.x0) * 100}%`,
            height: `${(region.y1 - region.y0) * 100}%`,
            clipPath: clipOf(region),
          };
          const mark = region.nid === selected ? "true" : undefined;
          return onSelect === null ? (
            <span
              key={`${region.nid}-${region.order}`}
              className="page-region pointer-events-none"
              data-selected={mark}
              style={box}
            />
          ) : (
            <button
              key={`${region.nid}-${region.order}`}
              type="button"
              title={`<${region.tag}> #${region.nid}\n${region.text.slice(0, 200)}`}
              onClick={(): void => onSelect(region.nid)}
              data-selected={mark}
              className="page-region"
              style={box}
            />
          );
        })}
      </div>
    </div>
  );
}

/** The page's own proportions, so the regions — fractions of the page, not pixels — land
 * where they belong at whatever size the frame happens to be. */
function ratioOf(page: {
  width: number | null;
  height: number | null;
}): string {
  return page.width && page.height
    ? `${page.width} / ${page.height}`
    : "1 / 1.414";
}

type ScanPaneProps = {
  page: PageOut | null;
  pages: PageSummaryOut[];
  selected: number | undefined;
  selectedPages: number[];
  onSelect: (nid: number) => void;
  onPage: (page: number) => void;
  /** Show this page on its own, over everything else. */
  onOpen: () => void;
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
  onOpen,
}: ScanPaneProps): JSX.Element {
  if (page === null) return <PaneBox>Loading the page...</PaneBox>;
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
        <PageImage
          page={page}
          selected={selected}
          onSelect={onSelect}
          ratio={ratioOf(page)}
          className="max-h-full max-w-full"
        />

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
            aria-label="Open the page larger"
            title="Open the page larger"
            onClick={onOpen}
          >
            <Maximize2 aria-hidden="true" />
          </Button>
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
/**
 * An EDTF estimate as a reader's date: "2026-08-05" → "05.08.2026", "2026-08" → "08.2026",
 * "2026-08-05/2026-08-10" → a span, "../2026-08-05" → "until …". The qualifiers EDTF allows
 * on an uncertain date (?, ~, %) say something the tooltip cannot, and are dropped.
 */
function edtfText(edtf: string): string {
  const cleaned = edtf.replace(/[?~%]/g, "").trim();
  if (cleaned === "") return "";
  const [from = "", to] = cleaned.split("/");
  if (to === undefined) return plainDate(from);
  if (from === "" || from === "..") return `until ${plainDate(to)}`;
  if (to === "" || to === "..") return `from ${plainDate(from)}`;
  return from === to
    ? plainDate(from)
    : `${plainDate(from)} - ${plainDate(to)}`;
}

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
