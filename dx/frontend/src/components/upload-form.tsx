import {
  type ChangeEvent,
  type DragEvent,
  type JSX,
  useId,
  useState,
} from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { formatBytes } from "@/lib/format";
import { cn } from "@/lib/utils";

type UploadFormProps = {
  title: string;
  /** The primary line in the drop zone — what the user is about to choose. */
  action?: string;
  /** `accept` attribute of the file input, e.g. "image/*,video/*". */
  accept?: string;
  onUpload: (files: File[]) => void;
  pending: boolean;
  error: string | null;
  uploadedCount: number | null;
};

/** Multi-file picker; tap to choose, plus drag & drop where there is a pointer. */
export function UploadForm({
  title,
  action = "Choose files",
  accept,
  onUpload,
  pending,
  error,
  uploadedCount,
}: UploadFormProps): JSX.Element {
  const inputId = useId();
  const [selected, setSelected] = useState<File[]>([]);
  const [dragging, setDragging] = useState(false);

  function addFiles(list: FileList | null): void {
    if (!list) return;
    // Read the list *now*, not inside the updater: clearing the input empties this very
    // FileList object (Chrome hands out the same one), and a drop's dataTransfer is neutered
    // when the handler returns. React runs the updater later whenever an update for this
    // state is already queued — a second pick then arrived as zero files, and nothing
    // happened.
    const incoming = Array.from(list);
    setSelected((current) => {
      const known = new Set(current.map((file) => `${file.name}:${file.size}`));
      const added = incoming.filter(
        (file) => !known.has(`${file.name}:${file.size}`),
      );
      return [...current, ...added];
    });
  }

  function handleInput(event: ChangeEvent<HTMLInputElement>): void {
    addFiles(event.target.files);
    event.target.value = "";
  }

  function handleDrop(event: DragEvent<HTMLElement>): void {
    event.preventDefault();
    setDragging(false);
    addFiles(event.dataTransfer.files);
  }

  function handleUpload(): void {
    if (selected.length === 0) return;
    onUpload(selected);
    setSelected([]);
  }

  return (
    <section className="flex flex-col gap-4 rounded-lg border p-4">
      <h2 className="font-medium">{title}</h2>
      <Label
        htmlFor={inputId}
        onDragOver={(event: DragEvent<HTMLElement>): void => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={(): void => setDragging(false)}
        onDrop={handleDrop}
        className={cn(
          "flex h-28 cursor-pointer flex-col items-center justify-center gap-1 rounded-md border-2 border-dashed px-4 text-center text-sm md:h-32",
          dragging && "border-primary bg-accent text-accent-foreground",
        )}
      >
        <span className="font-medium">{action}</span>
        {/* Dragging needs a pointer; a phone only ever taps, so the hint is desktop-only. */}
        <span className="hidden text-muted-foreground text-xs md:inline">
          or drop them here — several at once is fine
        </span>
      </Label>
      {/* Plain <input>: the styled Input sets w-full/h-8, which beat sr-only's 1px box and
          made the absolutely positioned element overflow the page horizontally. */}
      <input
        id={inputId}
        type="file"
        multiple
        accept={accept}
        onChange={handleInput}
        className="sr-only"
      />

      {selected.length > 0 && (
        <ul className="flex flex-col gap-1 text-sm">
          {selected.map((file) => (
            <li
              key={`${file.name}:${file.size}`}
              className="flex items-center justify-between gap-2"
            >
              <span className="min-w-0 truncate">
                {file.name}{" "}
                <span className="text-muted-foreground">
                  ({formatBytes(file.size)})
                </span>
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={(): void =>
                  setSelected((current) => current.filter((f) => f !== file))
                }
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:gap-4">
        <Button
          onClick={handleUpload}
          disabled={pending || selected.length === 0}
          className="w-full sm:w-auto"
        >
          {pending
            ? "Uploading..."
            : `Upload ${selected.length} file${selected.length === 1 ? "" : "s"}`}
        </Button>
        {uploadedCount !== null && !pending && !error && (
          <p className="text-muted-foreground text-sm">
            Uploaded {uploadedCount} file{uploadedCount === 1 ? "" : "s"}.
          </p>
        )}
        {error && (
          <p className="text-destructive text-sm">Upload failed: {error}</p>
        )}
      </div>
    </section>
  );
}
