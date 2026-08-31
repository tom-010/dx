import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import type { JSX } from "react";
import {
  getListMediaItemsQueryKey,
  useDeleteMediaItem,
  useListMediaItems,
  useUploadMediaItems,
} from "@/api/gallery/gallery";
import type { MediaItemOut } from "@/api/model";
import { Button } from "@/components/ui/button";
import { UploadForm } from "@/components/upload-form";
import { apiUrl, errorMessage } from "@/lib/custom-fetch";
import { formatBytes } from "@/lib/format";

export const Route = createFileRoute("/gallery")({
  component: GalleryPage,
});

function GalleryPage(): JSX.Element {
  const queryClient = useQueryClient();
  // Generated from openschema.json: list query + upload/delete mutations.
  const items = useListMediaItems();
  const invalidateList = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: getListMediaItemsQueryKey() });
  const upload = useUploadMediaItems({
    mutation: { onSuccess: invalidateList },
  });
  const remove = useDeleteMediaItem({
    mutation: { onSuccess: invalidateList },
  });

  return (
    <div className="flex flex-col gap-8">
      <h1 className="font-semibold text-2xl">Gallery</h1>

      <UploadForm
        title="Upload images and videos"
        hint="Drop images or videos here or click to choose (multiple allowed)"
        accept="image/*,video/*"
        onUpload={(files: File[]): void => upload.mutate({ data: { files } })}
        pending={upload.isPending}
        error={upload.isError ? errorMessage(upload.error) : null}
        uploadedCount={upload.isSuccess ? upload.data.length : null}
      />

      <section className="flex flex-col gap-2">
        {items.isPending && (
          <p className="text-muted-foreground">Loading gallery...</p>
        )}
        {items.isError && (
          <p className="text-destructive">
            Failed to load gallery: {errorMessage(items.error)}
          </p>
        )}
        {items.isSuccess && (
          <MediaGrid
            items={items.data.items}
            total={items.data.count}
            onDelete={(mediaItemId: string): void =>
              remove.mutate({ mediaItemId })
            }
            deletingId={remove.isPending ? remove.variables.mediaItemId : null}
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

type MediaGridProps = {
  items: MediaItemOut[];
  /** Total across all pages (the API is paginated; only the first page is shown). */
  total: number;
  onDelete: (id: string) => void;
  deletingId: string | null;
};

function MediaGrid({
  items,
  total,
  onDelete,
  deletingId,
}: MediaGridProps): JSX.Element {
  if (items.length === 0) {
    return (
      <p className="text-muted-foreground">
        Nothing here yet. Upload images or videos above.
      </p>
    );
  }

  return (
    <>
      {total > items.length && (
        <p className="text-muted-foreground text-sm">
          Showing {items.length} of {total}.
        </p>
      )}
      <ul className="grid grid-cols-2 gap-4 sm:grid-cols-3 lg:grid-cols-4">
        {items.map((item) => (
          <li
            key={item.id}
            className="flex flex-col gap-2 rounded-lg border p-2"
          >
            <MediaPreview item={item} />
            <div className="flex items-start justify-between gap-2 text-sm">
              <div className="min-w-0">
                <p className="truncate font-medium" title={item.name}>
                  {item.name}
                </p>
                <p className="text-muted-foreground text-xs">
                  {item.kind} · {formatBytes(item.size)} ·{" "}
                  {new Date(item.created).toLocaleDateString()}
                </p>
              </div>
              <Button asChild variant="ghost" size="sm">
                <Link
                  to="/history/$resource/$objectId"
                  params={{ resource: "mediaitem", objectId: item.id }}
                >
                  History
                </Link>
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={(): void => onDelete(item.id)}
                disabled={deletingId === item.id}
              >
                {deletingId === item.id ? "Deleting..." : "Delete"}
              </Button>
            </div>
          </li>
        ))}
      </ul>
    </>
  );
}

/** `item.url` is a signed link served by Django from the object store (see backend
 *  config/media.py); it works as a plain src, no auth header needed. `apiUrl` makes it
 *  absolute for the native build (same origin on the web, proxied by Vite in dev). */
function MediaPreview({ item }: { item: MediaItemOut }): JSX.Element {
  const src = apiUrl(item.url);
  if (item.kind === "video") {
    return (
      // biome-ignore lint/a11y/useMediaCaption: user uploads carry no caption tracks
      <video
        src={src}
        controls
        preload="metadata"
        className="aspect-square w-full rounded-md bg-muted object-contain"
      />
    );
  }
  return (
    <a href={src} target="_blank" rel="noreferrer">
      <img
        src={src}
        alt={item.name}
        loading="lazy"
        className="aspect-square w-full rounded-md bg-muted object-cover"
      />
    </a>
  );
}
