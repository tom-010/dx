import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { type ChangeEvent, type FormEvent, type JSX, useState } from "react";
import type { NoteOut } from "@/api/model";
import {
  getListNotesQueryKey,
  useCreateNote,
  useDeleteNote,
  useListNotes,
  useMergeNotes,
  usePatchNote,
} from "@/api/notes/notes";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { errorMessage } from "@/lib/custom-fetch";

export const Route = createFileRoute("/notes")({
  component: NotesPage,
});

// text-base below md: iOS zooms the whole page when a focused field is under 16px.
const TEXTAREA_CLASS =
  "w-full rounded-lg border border-input bg-transparent px-2.5 py-1.5 font-mono text-base outline-none transition-colors placeholder:text-muted-foreground focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50 md:text-sm dark:bg-input/30";

function NotesPage(): JSX.Element {
  const queryClient = useQueryClient();
  const notes = useListNotes();
  const invalidate = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: getListNotesQueryKey() });

  const create = useCreateNote({ mutation: { onSuccess: invalidate } });
  const patch = usePatchNote({ mutation: { onSuccess: invalidate } });
  const remove = useDeleteNote({ mutation: { onSuccess: invalidate } });
  const merge = useMergeNotes({ mutation: { onSuccess: invalidate } });

  const [selected, setSelected] = useState<string[]>([]);
  const [mergeTitle, setMergeTitle] = useState("");
  const [editing, setEditing] = useState<string | null>(null);

  const failed = [create, patch, remove, merge].find((m) => m.isError);

  function toggle(noteId: string): void {
    setSelected((current) =>
      current.includes(noteId)
        ? current.filter((id) => id !== noteId)
        : [...current, noteId],
    );
  }

  function handleMerge(): void {
    merge.mutate(
      {
        data: { note_ids: selected, title: mergeTitle.trim() || "Merged note" },
      },
      {
        onSuccess: () => {
          setSelected([]);
          setMergeTitle("");
        },
      },
    );
  }

  return (
    <div className="flex flex-col gap-6 md:gap-8">
      <div className="flex flex-col gap-1">
        <h1 className="font-semibold text-2xl">Notes</h1>
        <p className="text-muted-foreground text-sm">
          Every edit is a version — open “History” to see them. Merging two
          notes records which <em>version</em> of each one it read, which is
          what “Lineage” draws.
        </p>
      </div>

      <CreateNoteForm
        onSubmit={(title: string, body: string, tags: string): void =>
          create.mutate({ data: { title, body, tags } })
        }
        pending={create.isPending}
      />

      {failed && (
        <p className="text-destructive text-sm">{errorMessage(failed.error)}</p>
      )}

      {selected.length > 0 && (
        <div className="flex flex-wrap items-end gap-3 rounded-lg border border-dashed p-4 sm:gap-4">
          <div className="flex w-full flex-col gap-2 sm:w-auto">
            <Label htmlFor="merge-title">
              Merge {selected.length} selected note
              {selected.length === 1 ? "" : "s"} into
            </Label>
            <Input
              id="merge-title"
              value={mergeTitle}
              placeholder="Merged note"
              onChange={(event: ChangeEvent<HTMLInputElement>): void =>
                setMergeTitle(event.target.value)
              }
            />
          </div>
          <Button
            onClick={handleMerge}
            disabled={selected.length < 2 || merge.isPending}
          >
            {merge.isPending ? "Merging..." : "Merge"}
          </Button>
          <Button variant="ghost" onClick={(): void => setSelected([])}>
            Clear
          </Button>
          {selected.length < 2 && (
            <p className="text-muted-foreground text-sm">
              Select at least two notes to merge.
            </p>
          )}
        </div>
      )}

      <section className="flex flex-col gap-3">
        {notes.isPending && (
          <p className="text-muted-foreground">Loading notes...</p>
        )}
        {notes.isError && (
          <p className="text-destructive">
            Failed to load notes: {errorMessage(notes.error)}
          </p>
        )}
        {notes.isSuccess && notes.data.items.length === 0 && (
          <p className="text-muted-foreground">
            No notes yet. Create one above.
          </p>
        )}
        {notes.isSuccess &&
          notes.data.items.map((note) =>
            editing === note.id ? (
              <EditNoteForm
                key={note.id}
                note={note}
                pending={patch.isPending}
                onCancel={(): void => setEditing(null)}
                onSave={(title: string, body: string, tags: string): void =>
                  patch.mutate(
                    { noteId: note.id, data: { title, body, tags } },
                    { onSuccess: () => setEditing(null) },
                  )
                }
              />
            ) : (
              <NoteCard
                key={note.id}
                note={note}
                selected={selected.includes(note.id)}
                onToggle={(): void => toggle(note.id)}
                onEdit={(): void => setEditing(note.id)}
                onDelete={(): void => remove.mutate({ noteId: note.id })}
                busy={remove.isPending}
              />
            ),
          )}
      </section>
    </div>
  );
}

function TagChips({ tags }: { tags: string }): JSX.Element | null {
  const list = tags
    .split(",")
    .map((tag) => tag.trim())
    .filter((tag) => tag !== "");
  if (list.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1">
      {list.map((tag) => (
        <span
          key={tag}
          className="rounded bg-muted px-2 py-0.5 text-muted-foreground text-xs"
        >
          {tag}
        </span>
      ))}
    </div>
  );
}

type NoteCardProps = {
  note: NoteOut;
  selected: boolean;
  onToggle: () => void;
  onEdit: () => void;
  onDelete: () => void;
  busy: boolean;
};

function NoteCard({
  note,
  selected,
  onToggle,
  onEdit,
  onDelete,
  busy,
}: NoteCardProps): JSX.Element {
  return (
    <article className="flex flex-col gap-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <div className="flex min-w-0 items-center gap-3">
          <input
            type="checkbox"
            checked={selected}
            onChange={onToggle}
            aria-label={`Select ${note.title} for merging`}
            /* size-5 below md: a checkbox is the smallest thing on the page to hit. */
            className="size-5 shrink-0 md:size-4"
          />
          <h2 className="min-w-0 break-words font-medium">{note.title}</h2>
          <span className="shrink-0 rounded bg-muted px-2 py-0.5 text-muted-foreground text-xs">
            v{note.version}
          </span>
        </div>
        <time
          className="text-muted-foreground text-xs sm:text-sm"
          dateTime={note.modified}
        >
          {new Date(note.modified).toLocaleString()}
        </time>
      </div>

      {note.body && (
        <pre className="overflow-x-auto whitespace-pre-wrap font-sans text-muted-foreground text-sm">
          {note.body}
        </pre>
      )}

      <TagChips tags={note.tags} />

      <div className="flex flex-wrap gap-1">
        <Button variant="ghost" size="sm" onClick={onEdit} disabled={busy}>
          Edit
        </Button>
        <Button asChild variant="ghost" size="sm">
          <Link
            to="/lineage/$resource/$objectId"
            params={{ resource: "note", objectId: note.id }}
          >
            Lineage
          </Link>
        </Button>
        <Button asChild variant="ghost" size="sm">
          <Link
            to="/history/$resource/$objectId"
            params={{ resource: "note", objectId: note.id }}
          >
            History
          </Link>
        </Button>
        <Button variant="ghost" size="sm" onClick={onDelete} disabled={busy}>
          Delete
        </Button>
      </div>
    </article>
  );
}

type NoteFieldsProps = {
  title: string;
  body: string;
  tags: string;
  idPrefix: string;
  onTitle: (value: string) => void;
  onBody: (value: string) => void;
  onTags: (value: string) => void;
};

/** The three inputs a note has, shared by the create form and the edit form. */
function NoteFields({
  title,
  body,
  tags,
  idPrefix,
  onTitle,
  onBody,
  onTags,
}: NoteFieldsProps): JSX.Element {
  return (
    <>
      <div className="flex flex-col gap-2">
        <Label htmlFor={`${idPrefix}-title`}>Title</Label>
        <Input
          id={`${idPrefix}-title`}
          value={title}
          onChange={(event: ChangeEvent<HTMLInputElement>): void =>
            onTitle(event.target.value)
          }
          maxLength={200}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor={`${idPrefix}-body`}>Body</Label>
        <textarea
          id={`${idPrefix}-body`}
          value={body}
          rows={5}
          onChange={(event: ChangeEvent<HTMLTextAreaElement>): void =>
            onBody(event.target.value)
          }
          className={TEXTAREA_CLASS}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor={`${idPrefix}-tags`}>Tags</Label>
        <Input
          id={`${idPrefix}-tags`}
          value={tags}
          placeholder="walk, birds"
          onChange={(event: ChangeEvent<HTMLInputElement>): void =>
            onTags(event.target.value)
          }
          maxLength={500}
        />
        <p className="text-muted-foreground text-xs">
          Comma-separated. Tidied up on save, so the note's history only shows a
          change when the set really changed.
        </p>
      </div>
    </>
  );
}

function CreateNoteForm({
  onSubmit,
  pending,
}: {
  onSubmit: (title: string, body: string, tags: string) => void;
  pending: boolean;
}): JSX.Element {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [tags, setTags] = useState("");

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (title.trim() === "") return;
    onSubmit(title.trim(), body, tags);
    setTitle("");
    setBody("");
    setTags("");
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-4 rounded-lg border p-4"
    >
      <h2 className="font-medium">New note</h2>
      <NoteFields
        title={title}
        body={body}
        tags={tags}
        idPrefix="new-note"
        onTitle={setTitle}
        onBody={setBody}
        onTags={setTags}
      />
      <div>
        <Button type="submit" disabled={pending}>
          {pending ? "Creating..." : "Create"}
        </Button>
      </div>
    </form>
  );
}

function EditNoteForm({
  note,
  pending,
  onSave,
  onCancel,
}: {
  note: NoteOut;
  pending: boolean;
  onSave: (title: string, body: string, tags: string) => void;
  onCancel: () => void;
}): JSX.Element {
  const [title, setTitle] = useState(note.title);
  const [body, setBody] = useState(note.body);
  const [tags, setTags] = useState(note.tags);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    if (title.trim() === "") return;
    onSave(title.trim(), body, tags);
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-4 rounded-lg border border-foreground p-4"
    >
      <div className="flex items-baseline justify-between gap-2">
        <h2 className="font-medium">Editing “{note.title}”</h2>
        <span className="text-muted-foreground text-xs">
          saving creates v{note.version + 1}
        </span>
      </div>
      <NoteFields
        title={title}
        body={body}
        tags={tags}
        idPrefix={`edit-${note.id}`}
        onTitle={setTitle}
        onBody={setBody}
        onTags={setTags}
      />
      <div className="flex gap-2">
        <Button type="submit" disabled={pending}>
          {pending ? "Saving..." : "Save"}
        </Button>
        <Button type="button" variant="ghost" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </form>
  );
}
