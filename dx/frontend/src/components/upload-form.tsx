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
  /** Drop-zone text. */
  hint?: string;
  /** `accept` attribute of the file input, e.g. "image/*,video/*". */
  accept?: string;
  onUpload: (files: File[]) => void;
  pending: boolean;
  error: string | null;
  uploadedCount: number | null;
};

/** Multi-file picker with drag & drop; the caller owns the upload mutation. */
export function UploadForm({
  title,
  hint = "Drop files here or click to choose (multiple allowed)",
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
    setSelected((current) => {
      const known = new Set(current.map((file) => `${file.name}:${file.size}`));
      const added = Array.from(list).filter(
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
          "flex h-32 cursor-pointer items-center justify-center rounded-md border-2 border-dashed text-center text-muted-foreground text-sm",
          dragging && "border-primary bg-accent text-accent-foreground",
        )}
      >
        {hint}
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
              className="flex items-center justify-between gap-4"
            >
              <span>
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

      <div className="flex items-center gap-4">
        <Button
          onClick={handleUpload}
          disabled={pending || selected.length === 0}
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
