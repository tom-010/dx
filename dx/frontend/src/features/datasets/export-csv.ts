/**
 * CSV export for datasets.
 *
 * This module is intentionally NOT imported statically anywhere. The
 * datasets page loads it with `await import(...)` inside the click handler,
 * so it becomes its own chunk that is only fetched when the user exports.
 * Heavy libraries (Excel, PDF, charts, ...) follow the same pattern.
 */

import type { DatasetOut } from "@/api/model";

const COLUMNS = ["id", "name", "description", "row_count", "created"] as const;

function escapeCell(value: unknown): string {
  const text = value === null || value === undefined ? "" : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

export function datasetsToCsv(datasets: DatasetOut[]): string {
  const header = COLUMNS.join(",");
  const rows = datasets.map((dataset) =>
    COLUMNS.map((column) => escapeCell(dataset[column])).join(","),
  );
  return [header, ...rows].join("\r\n");
}

export function exportDatasetsCsv(
  datasets: DatasetOut[],
  filename = "datasets.csv",
): void {
  const blob = new Blob([datasetsToCsv(datasets)], {
    type: "text/csv;charset=utf-8",
  });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
