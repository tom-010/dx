import { useQueryClient } from "@tanstack/react-query";
import { createFileRoute, Link } from "@tanstack/react-router";
import type { JSX } from "react";
import {
  getListDatasetsQueryKey,
  useImportDatasetFromDocument,
} from "@/api/datasets/datasets";
import {
  getListDocumentsQueryKey,
  useDeleteDocument,
  useListDocuments,
  useUploadDocuments,
} from "@/api/documents/documents";
import type { DocumentOut } from "@/api/model";
import { Button } from "@/components/ui/button";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { UploadForm } from "@/components/upload-form";
import { errorMessage } from "@/lib/custom-fetch";
import { formatBytes } from "@/lib/format";

export const Route = createFileRoute("/documents")({
  component: DocumentsPage,
});

function DocumentsPage(): JSX.Element {
  const queryClient = useQueryClient();
  // Generated from openschema.json: list query + upload/delete mutations.
  const documents = useListDocuments();
  const invalidateList = (): Promise<void> =>
    queryClient.invalidateQueries({ queryKey: getListDocumentsQueryKey() });
  const upload = useUploadDocuments({
    mutation: { onSuccess: invalidateList },
  });
  const remove = useDeleteDocument({
    mutation: { onSuccess: invalidateList },
  });
  // Importing builds a dataset from this document and records where it came from; the datasets
  // list has to be refetched because a new one appeared there.
  const importDocument = useImportDatasetFromDocument({
    mutation: {
      onSuccess: (): Promise<void> =>
        queryClient.invalidateQueries({ queryKey: getListDatasetsQueryKey() }),
    },
  });

  return (
    <div className="flex flex-col gap-8">
      <h1 className="font-semibold text-2xl">Documents</h1>

      <UploadForm
        title="Upload documents"
        onUpload={(files: File[]): void => upload.mutate({ data: { files } })}
        pending={upload.isPending}
        error={upload.isError ? errorMessage(upload.error) : null}
        uploadedCount={upload.isSuccess ? upload.data.length : null}
      />

      <section className="flex flex-col gap-2">
        {documents.isPending && (
          <p className="text-muted-foreground">Loading documents...</p>
        )}
        {documents.isError && (
          <p className="text-destructive">
            Failed to load documents: {errorMessage(documents.error)}
          </p>
        )}
        {importDocument.isError && (
          <p className="text-destructive text-sm">
            Import failed: {errorMessage(importDocument.error)}
          </p>
        )}
        {importDocument.isSuccess && (
          <p className="text-muted-foreground text-sm">
            Imported <strong>{importDocument.data.name}</strong> —{" "}
            {importDocument.data.row_count.toLocaleString()} rows.{" "}
            <Link
              to="/history/$resource/$objectId"
              params={{ resource: "dataset", objectId: importDocument.data.id }}
              className="underline underline-offset-4"
            >
              See what it was built from
            </Link>
          </p>
        )}
        {documents.isSuccess && (
          <DocumentTable
            documents={documents.data.items}
            onImport={(documentId: string): void =>
              importDocument.mutate({ data: { document_id: documentId } })
            }
            importingId={
              importDocument.isPending
                ? importDocument.variables.data.document_id
                : null
            }
            onDelete={(documentId: string): void =>
              remove.mutate({ documentId })
            }
            deletingId={remove.isPending ? remove.variables.documentId : null}
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

type DocumentTableProps = {
  documents: DocumentOut[];
  onDelete: (id: string) => void;
  deletingId: string | null;
  onImport: (id: string) => void;
  importingId: string | null;
};

function DocumentTable({
  documents,
  onDelete,
  deletingId,
  onImport,
  importingId,
}: DocumentTableProps): JSX.Element {
  if (documents.length === 0) {
    return (
      <p className="text-muted-foreground">
        No documents yet. Upload some above.
      </p>
    );
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead>Name</TableHead>
          <TableHead>Type</TableHead>
          <TableHead className="text-right">Size</TableHead>
          <TableHead>Uploaded</TableHead>
          <TableHead className="w-56" />
        </TableRow>
      </TableHeader>
      <TableBody>
        {documents.map((document) => (
          <TableRow key={document.id}>
            <TableCell className="font-medium">{document.name}</TableCell>
            <TableCell className="text-muted-foreground">
              {document.content_type || "—"}
            </TableCell>
            <TableCell className="text-right tabular-nums">
              {formatBytes(document.size)}
            </TableCell>
            <TableCell className="text-muted-foreground">
              {new Date(document.created).toLocaleString()}
            </TableCell>
            <TableCell className="text-right">
              <Button variant="ghost" size="sm" asChild>
                <a href={document.download_url} download={document.name}>
                  Download
                </a>
              </Button>
              <Button asChild variant="ghost" size="sm">
                <Link
                  to="/history/$resource/$objectId"
                  params={{ resource: "document", objectId: document.id }}
                >
                  History
                </Link>
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={(): void => onImport(document.id)}
                disabled={importingId === document.id}
                title="Create a dataset from this file"
              >
                {importingId === document.id ? "Importing..." : "Import"}
              </Button>
              <Button
                variant="ghost"
                size="sm"
                onClick={(): void => onDelete(document.id)}
                disabled={deletingId === document.id}
              >
                {deletingId === document.id ? "Deleting..." : "Delete"}
              </Button>
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
