import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute } from "@tanstack/react-router";
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
    <div className="flex flex-col gap-8">
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
  const [validationError, setValidationError] = useState<string | null>(null);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    // Client-side validation with the Zod schema generated from the same spec.
    const parsed = CreateDatasetBody.safeParse({
      name: name.trim(),
      description: description.trim(),
      row_count: rowCount === "" ? undefined : Number(rowCount),
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
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-4 rounded-lg border p-4"
    >
      <h2 className="font-medium">New dataset</h2>
      <div className="grid gap-4 sm:grid-cols-4">
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
      <div className="flex items-center gap-4">
        <Button type="submit" disabled={pending}>
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
          <TableHead className="w-24">ID</TableHead>
          <TableHead>Name</TableHead>
          <TableHead>Description</TableHead>
          <TableHead className="text-right">Rows</TableHead>
          <TableHead>Delimiter</TableHead>
          <TableHead>Created</TableHead>
          <TableHead className="w-24" />
        </TableRow>
      </TableHeader>
      <TableBody>
        {datasets.map((dataset) => (
          <TableRow key={dataset.id}>
            {/* UUIDv7: the first block is the timestamp, enough to tell rows apart. */}
            <TableCell className="font-mono text-xs" title={dataset.id}>
              {dataset.id.slice(0, 8)}
            </TableCell>
            <TableCell className="font-medium">{dataset.name}</TableCell>
            <TableCell className="text-muted-foreground">
              {dataset.description}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {dataset.row_count.toLocaleString()}
            </TableCell>
            <TableCell className="font-mono text-muted-foreground">
              {dataset.options.delimiter === "\t"
                ? "tab"
                : dataset.options.delimiter}
            </TableCell>
            <TableCell className="text-muted-foreground">
              {new Date(dataset.created).toLocaleString()}
            </TableCell>
            <TableCell className="text-right">
              <Button
                variant="ghost"
                size="sm"
                onClick={(): void => onDelete(dataset.id)}
                disabled={deletingId === dataset.id}
              >
                {deletingId === dataset.id ? "Deleting..." : "Delete"}
              </Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
