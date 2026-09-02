import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import { type ChangeEvent, type FormEvent, type JSX, useState } from "react";
import {
  getListDatasetsQueryKey,
  useCreateDataset,
  useDeleteDataset,
  useListDatasets,
  usePatchDataset,
} from "@/api/datasets/datasets";
import type {
  DatasetIn,
  DatasetOptions,
  DatasetOut,
  DatasetPatch,
} from "@/api/model";
import {
  CreateDatasetBody,
  PatchDatasetBody,
} from "@/api/zod/datasets/datasets";
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

type DatasetsSearch = { edit?: string };

export const Route = createFileRoute("/datasets")({
  // The open editor is URL state, not React state: a refresh (or a shared link) reopens it.
  validateSearch: (search: Record<string, unknown>): DatasetsSearch => ({
    edit: typeof search.edit === "string" ? search.edit : undefined,
  }),
  component: DatasetsPage,
});

function DatasetsPage(): JSX.Element {
  const queryClient = useQueryClient();
  // Everything below `@/api/` is generated from openschema.json — never edited by hand.
  const datasets = useListDatasets();
  const { edit } = Route.useSearch();
  const navigate = Route.useNavigate();
  const invalidateList = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: getListDatasetsQueryKey() });
  const create = useCreateDataset({
    mutation: { onSuccess: invalidateList },
  });
  const patch = usePatchDataset({
    mutation: { onSuccess: invalidateList },
  });
  const remove = useDeleteDataset({
    mutation: { onSuccess: invalidateList },
  });

  const [exportError, setExportError] = useState<string | null>(null);

  function setEditing(datasetId: string | undefined): void {
    navigate({ to: "/datasets", search: { edit: datasetId } });
  }

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
            editingId={edit ?? null}
            onEdit={setEditing}
            onCancelEdit={(): void => setEditing(undefined)}
            onSave={(datasetId: string, data: DatasetPatch): void =>
              patch.mutate(
                { datasetId, data },
                { onSuccess: (): void => setEditing(undefined) },
              )
            }
            savingId={patch.isPending ? patch.variables.datasetId : null}
            onDelete={(datasetId: string): void => remove.mutate({ datasetId })}
            deletingId={remove.isPending ? remove.variables.datasetId : null}
          />
        )}
        {patch.isError && (
          <p className="text-destructive text-sm">
            Save failed: {errorMessage(patch.error)}
          </p>
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

/** The five editable fields as strings, the shape both forms hold while they are being typed in. */
type DatasetFormValues = {
  name: string;
  description: string;
  rowCount: string;
  delimiter: string;
  tags: string;
};

const EMPTY_FORM: DatasetFormValues = {
  name: "",
  description: "",
  rowCount: "",
  delimiter: ",",
  tags: "",
};

function formValues(dataset: DatasetOut): DatasetFormValues {
  return {
    name: dataset.name,
    description: dataset.description,
    rowCount: String(dataset.row_count),
    delimiter: dataset.options.delimiter ?? ",",
    tags: dataset.tags.join(", "),
  };
}

type DatasetPayload = {
  name: string;
  description: string;
  row_count: number | undefined;
  tags: string[];
  options: DatasetOptions;
};

/**
 * The form as the API wants it. `options` is a typed JSON column and is replaced whole on
 * both PUT and PATCH, so the edit form passes the dataset's current options in: the fields
 * this form does not show (encoding, has_header) travel back unchanged.
 */
function datasetPayload(
  values: DatasetFormValues,
  options?: DatasetOptions,
): DatasetPayload {
  return {
    name: values.name.trim(),
    description: values.description.trim(),
    row_count: values.rowCount === "" ? undefined : Number(values.rowCount),
    // Tags live in an owned join model on the backend, so they travel with the dataset and
    // land in the same revision as the rest of this form.
    tags: values.tags
      .split(",")
      .map((tag) => tag.trim())
      .filter((tag) => tag !== ""),
    options: { ...options, delimiter: values.delimiter },
  };
}

function firstIssue(
  issues: readonly { path: PropertyKey[]; message: string }[],
): string {
  const issue = issues[0];
  return issue ? `${issue.path.join(".")}: ${issue.message}` : "Invalid input";
}

type DatasetFieldsProps = {
  values: DatasetFormValues;
  idPrefix: string;
  onChange: (values: DatasetFormValues) => void;
};

/** The inputs a dataset has, shared by the create form and the edit form. */
function DatasetFields({
  values,
  idPrefix,
  onChange,
}: DatasetFieldsProps): JSX.Element {
  function set(
    field: keyof DatasetFormValues,
  ): (event: ChangeEvent<HTMLInputElement>) => void {
    return (event: ChangeEvent<HTMLInputElement>): void =>
      onChange({ ...values, [field]: event.target.value });
  }

  return (
    <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
      <div className="flex flex-col gap-2">
        <Label htmlFor={`${idPrefix}-name`}>Name</Label>
        <Input
          id={`${idPrefix}-name`}
          value={values.name}
          onChange={set("name")}
          maxLength={200}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor={`${idPrefix}-description`}>Description</Label>
        <Input
          id={`${idPrefix}-description`}
          value={values.description}
          onChange={set("description")}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor={`${idPrefix}-row-count`}>Row count</Label>
        <Input
          id={`${idPrefix}-row-count`}
          type="number"
          min={0}
          step={1}
          value={values.rowCount}
          onChange={set("rowCount")}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor={`${idPrefix}-tags`}>Tags</Label>
        <Input
          id={`${idPrefix}-tags`}
          value={values.tags}
          placeholder="sales, 2026"
          onChange={set("tags")}
        />
      </div>
      <div className="flex flex-col gap-2">
        <Label htmlFor={`${idPrefix}-delimiter`}>Delimiter</Label>
        <Input
          id={`${idPrefix}-delimiter`}
          value={values.delimiter}
          onChange={set("delimiter")}
          maxLength={1}
        />
      </div>
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
  const [values, setValues] = useState<DatasetFormValues>(EMPTY_FORM);
  const [validationError, setValidationError] = useState<string | null>(null);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    // Client-side validation with the Zod schema generated from the same spec.
    const parsed = CreateDatasetBody.safeParse(datasetPayload(values));
    if (!parsed.success) {
      setValidationError(firstIssue(parsed.error.issues));
      return;
    }
    setValidationError(null);
    onSubmit(parsed.data);
    setValues(EMPTY_FORM);
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-4 rounded-lg border p-4"
    >
      <h2 className="font-medium">New dataset</h2>
      <DatasetFields values={values} idPrefix="dataset" onChange={setValues} />
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
  editingId: string | null;
  onEdit: (id: string) => void;
  onCancelEdit: () => void;
  onSave: (id: string, data: DatasetPatch) => void;
  savingId: string | null;
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
  editingId,
  onEdit,
  onCancelEdit,
  onSave,
  savingId,
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
        {datasets.map((dataset) =>
          dataset.id === editingId ? (
            <EditDatasetRow
              key={dataset.id}
              dataset={dataset}
              pending={savingId === dataset.id}
              onSave={(data: DatasetPatch): void => onSave(dataset.id, data)}
              onCancel={onCancelEdit}
            />
          ) : (
            <DatasetRow
              key={dataset.id}
              dataset={dataset}
              onEdit={(): void => onEdit(dataset.id)}
              onDelete={(): void => onDelete(dataset.id)}
              deleting={deletingId === dataset.id}
            />
          ),
        )}
      </TableBody>
    </Table>
  );
}

type DatasetRowProps = {
  dataset: DatasetOut;
  onEdit: () => void;
  onDelete: () => void;
  deleting: boolean;
};

function DatasetRow({
  dataset,
  onEdit,
  onDelete,
  deleting,
}: DatasetRowProps): JSX.Element {
  return (
    <TableRow>
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
          <Button
            variant="ghost"
            size="sm"
            onClick={onEdit}
            disabled={deleting}
          >
            Edit
          </Button>
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
            onClick={onDelete}
            disabled={deleting}
          >
            {deleting ? "Deleting..." : "Delete"}
          </Button>
        </div>
      </TableCell>
    </TableRow>
  );
}

type EditDatasetRowProps = {
  dataset: DatasetOut;
  pending: boolean;
  onSave: (data: DatasetPatch) => void;
  onCancel: () => void;
};

/**
 * The edited row, as a form across the whole width. A PATCH, so only what this form shows
 * is written — the save lands as one new version of the dataset ("History").
 */
function EditDatasetRow({
  dataset,
  pending,
  onSave,
  onCancel,
}: EditDatasetRowProps): JSX.Element {
  const [values, setValues] = useState<DatasetFormValues>(() =>
    formValues(dataset),
  );
  const [validationError, setValidationError] = useState<string | null>(null);

  function handleSubmit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const parsed = PatchDatasetBody.safeParse(
      datasetPayload(values, dataset.options),
    );
    if (!parsed.success) {
      setValidationError(firstIssue(parsed.error.issues));
      return;
    }
    setValidationError(null);
    onSave(parsed.data);
  }

  return (
    <TableRow>
      <TableCell colSpan={8} className="p-4">
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <h2 className="font-medium">
            Edit “{dataset.name}”{" "}
            <span className="font-normal text-muted-foreground text-xs">
              v{dataset.version}
            </span>
          </h2>
          <DatasetFields
            values={values}
            idPrefix={`dataset-${dataset.id}`}
            onChange={setValues}
          />
          <div className="flex flex-col items-start gap-2 sm:flex-row sm:items-center sm:gap-4">
            <div className="flex gap-2">
              <Button type="submit" disabled={pending}>
                {pending ? "Saving..." : "Save"}
              </Button>
              <Button type="button" variant="ghost" onClick={onCancel}>
                Cancel
              </Button>
            </div>
            {validationError && (
              <p className="text-destructive text-sm">{validationError}</p>
            )}
          </div>
        </form>
      </TableCell>
    </TableRow>
  );
}
