import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { type ChangeEvent, type FormEvent, type JSX, useState } from "react";
import {
  getListDatasetsQueryKey,
  useCreateDataset,
  useDeleteDataset,
  useListDatasets,
} from "@/api/datasets/datasets";
import type { DatasetIn, DatasetOut } from "@/api/model";
import { CreateDatasetBody } from "@/api/zod/datasets/datasets";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { errorMessage } from "@/lib/custom-fetch";

export const Route = createFileRoute("/datasets")({
  component: DatasetsPage,
});

function DatasetsPage(): JSX.Element {
  const queryClient = useQueryClient();
  // Everything below `@/api/` is generated from openschema.json — never edited by hand.
  const datasets = useListDatasets();
  const invalidateList = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: getListDatasetsQueryKey() });
  const create = useCreateDataset({
    mutation: { onSuccess: invalidateList },
  });
  const remove = useDeleteDataset({
    mutation: { onSuccess: invalidateList },
  });

  const [exportError, setExportError] = useState<string | null>(null);

  async function handleExport(): Promise<void> {
    if (!datasets.data) return;
    setExportError(null);
    try {
      // Loaded on demand: this module (and any heavy library it pulls in)
      // is a separate chunk that is only downloaded when the user exports.
      const { exportDatasetsCsv } = await import(
        "@/features/datasets/export-csv"
      );
      exportDatasetsCsv(datasets.data.items);
    } catch (error) {
      setExportError(errorMessage(error));
    }
  }

  return (
    <div className="flex flex-col gap-6 md:gap-8">
      <div className="flex items-center justify-between gap-4">
        <h1 className="font-semibold text-2xl">Datasets</h1>
        <Button
          variant="outline"
          onClick={handleExport}
          disabled={!datasets.data || datasets.data.items.length === 0}
        >
          Export CSV
        </Button>
      </div>
      {exportError && (
        <p className="text-destructive text-sm">Export failed: {exportError}</p>
      )}

      <CreateDatasetForm
        onSubmit={(data: DatasetIn): void => create.mutate({ data })}
        pending={create.isPending}
        error={create.isError ? errorMessage(create.error) : null}
      />

      <section className="flex flex-col gap-2">
        {datasets.isPending && (
          <p className="text-muted-foreground">Loading datasets...</p>
        )}
        {datasets.isError && (
          <p className="text-destructive">
            Failed to load datasets: {errorMessage(datasets.error)}
          </p>
        )}
        {datasets.isSuccess && (
          <DatasetTable
            datasets={datasets.data.items}
            onDelete={(datasetId: string): void => remove.mutate({ datasetId })}
            deletingId={remove.isPending ? remove.variables.datasetId : null}
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

type CreateDatasetFormProps = {
  onSubmit: (data: DatasetIn) => void;
  pending: boolean;
  error: string | null;
};

function CreateDatasetForm({
  onSubmit,
  pending,
  error,
}: CreateDatasetFormProps): JSX.Element {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [rowCount, setRowCount] = useState("");
  const [delimiter, setDelimiter] = useState(",");
  const [tags, setTags] = useState("");
  const [validationError, setValidationError] = useState<string | null>(null);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    // Client-side validation with the Zod schema generated from the same spec.
    const parsed = CreateDatasetBody.safeParse({
      name: name.trim(),
      description: description.trim(),
      row_count: rowCount === "" ? undefined : Number(rowCount),
      // Tags live in an owned join model on the backend, so they travel with the dataset and
      // land in the same revision as the rest of this form.
      tags: tags
        .split(",")
        .map((tag) => tag.trim())
        .filter((tag) => tag !== ""),
      // `options` is a typed JSON column on the backend (DatasetOptions); the same pydantic
      // model shows up here as a nested object in the generated types and Zod schema.
      options: { delimiter },
    });
    if (!parsed.success) {
      const issue = parsed.error.issues[0];
      setValidationError(
        issue ? `${issue.path.join(".")}: ${issue.message}` : "Invalid input",
      );
      return;
    }
    setValidationError(null);
    onSubmit(parsed.data);
    setName("");
    setDescription("");
    setRowCount("");
    setDelimiter(",");
    setTags("");
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-4 rounded-lg border p-4"
    >
      <h2 className="font-medium">New dataset</h2>
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <div className="flex flex-col gap-2">
          <Label htmlFor="dataset-name">Name</Label>
          <Input
            id="dataset-name"
            value={name}
            onChange={(event: ChangeEvent<HTMLInputElement>): void =>
              setName(event.target.value)
            }
            maxLength={200}
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="dataset-description">Description</Label>
          <Input
            id="dataset-description"
            value={description}
            onChange={(event: ChangeEvent<HTMLInputElement>): void =>
              setDescription(event.target.value)
            }
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="dataset-row-count">Row count</Label>
          <Input
            id="dataset-row-count"
            type="number"
            min={0}
            step={1}
            value={rowCount}
            onChange={(event: ChangeEvent<HTMLInputElement>): void =>
              setRowCount(event.target.value)
            }
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="dataset-tags">Tags</Label>
          <Input
            id="dataset-tags"
            value={tags}
            placeholder="sales, 2026"
            onChange={(event: ChangeEvent<HTMLInputElement>): void =>
              setTags(event.target.value)
            }
          />
        </div>
        <div className="flex flex-col gap-2">
          <Label htmlFor="dataset-delimiter">Delimiter</Label>
          <Input
            id="dataset-delimiter"
            value={delimiter}
            onChange={(event: ChangeEvent<HTMLInputElement>): void =>
              setDelimiter(event.target.value)
            }
            maxLength={1}
          />
        </div>
      </div>
      <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:gap-4">
        <Button type="submit" disabled={pending} className="w-full sm:w-auto">
          {pending ? "Creating..." : "Create"}
        </Button>
        {validationError && (
          <p className="text-destructive text-sm">{validationError}</p>
        )}
        {error && (
          <p className="text-destructive text-sm">Create failed: {error}</p>
        )}
      </div>
    </form>
  );
}

type DatasetTableProps = {
  datasets: DatasetOut[];
  onDelete: (id: string) => void;
  deletingId: string | null;
};

// `delimiter` is optional in the generated type (ninja ModelSchema gives fields with a
// default `?`), so an absent one renders as nothing, exactly as before.
function delimiterLabel(delimiter: string | undefined): string | undefined {
  return delimiter === "\t" ? "tab" : delimiter;
}

function TagChips({ tags }: { tags: string[] }): JSX.Element {
  return (
    <div className="flex flex-wrap gap-1">
      {tags.map((tag) => (
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

/**
 * One breakpoint decides the whole shape: below `lg` a phone or a portrait tablet gets the
 * name with everything else stacked under it, from `lg` up the full table. Two layouts of
 * the same row, not two components — the columns are simply not rendered on a small screen.
 */
function DatasetTable({
  datasets,
  onDelete,
  deletingId,
}: DatasetTableProps): JSX.Element {
  if (datasets.length === 0) {
    return (
      <p className="text-muted-foreground">
        No datasets yet. Create one above.
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="hidden w-24 lg:table-cell">ID</TableHead>
          <TableHead>Name</TableHead>
          <TableHead className="hidden lg:table-cell">Description</TableHead>
          <TableHead className="hidden text-right lg:table-cell">
            Rows
          </TableHead>
          <TableHead className="hidden lg:table-cell">Tags</TableHead>
          <TableHead className="hidden lg:table-cell">Delimiter</TableHead>
          <TableHead className="hidden lg:table-cell">Created</TableHead>
          <TableHead />
        </TableRow>
      </TableHeader>
      <TableBody>
        {datasets.map((dataset) => (
          <TableRow key={dataset.id}>
            {/* UUIDv7: the first block is the timestamp, enough to tell rows apart. */}
            <TableCell
              className="hidden font-mono text-xs lg:table-cell"
              title={dataset.id}
            >
              {dataset.id.slice(0, 8)}
            </TableCell>
            <TableCell className="whitespace-normal font-medium lg:whitespace-nowrap">
              {dataset.name}
              {/* Everything the small screen hides, restated under the name. */}
              <div className="mt-1 flex flex-col gap-1 font-normal text-muted-foreground text-xs lg:hidden">
                {dataset.description && <span>{dataset.description}</span>}
                <span className="tabular-nums">
                  {dataset.row_count.toLocaleString()} rows ·{" "}
                  {delimiterLabel(dataset.options.delimiter)} ·{" "}
                  {new Date(dataset.created).toLocaleDateString()}
                </span>
                {dataset.tags.length > 0 && <TagChips tags={dataset.tags} />}
              </div>
            </TableCell>
            <TableCell className="hidden text-muted-foreground lg:table-cell">
              {dataset.description}
            </TableCell>
            <TableCell className="hidden text-right tabular-nums lg:table-cell">
              {dataset.row_count.toLocaleString()}
            </TableCell>
            <TableCell className="hidden lg:table-cell">
              <TagChips tags={dataset.tags} />
            </TableCell>
            <TableCell className="hidden font-mono text-muted-foreground lg:table-cell">
              {delimiterLabel(dataset.options.delimiter)}
            </TableCell>
            <TableCell className="hidden text-muted-foreground lg:table-cell">
              {new Date(dataset.created).toLocaleString()}
            </TableCell>
            <TableCell>
              <div className="flex flex-wrap justify-end gap-1 lg:flex-nowrap">
                <Button asChild variant="ghost" size="sm">
                  <Link
                    to="/history/$resource/$objectId"
                    params={{ resource: "dataset", objectId: dataset.id }}
                  >
                    History
                  </Link>
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={(): void => onDelete(dataset.id)}
                  disabled={deletingId === dataset.id}
                >
                  {deletingId === dataset.id ? "Deleting..." : "Delete"}
                </Button>
              </div>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
