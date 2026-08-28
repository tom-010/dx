// OpenAPI → TypeScript pipeline (NOTES.md §4). Paths are relative to this file.
// Run via ./scripts/sync_schema.sh (or frontend/sync_schema.sh / `pnpm sync-schema`): exports the spec from Django, then runs orval.
import { defineConfig } from "orval";

const INPUT = "./openschema.json";

export default defineConfig({
  // Typed fetchers + TanStack Query hooks, one folder per OpenAPI tag, models in model/.
  api: {
    input: { target: INPUT },
    output: {
      target: "./frontend/src/api/endpoints.ts",
      schemas: "./frontend/src/api/model",
      mode: "tags-split",
      client: "react-query",
      httpClient: "fetch",
      clean: true,
      override: {
        // All requests go through customFetch (base URL, JSON parsing, throwing ApiError).
        mutator: { path: "./frontend/src/lib/custom-fetch.ts", name: "customFetch" },
        fetch: { includeHttpResponseReturnType: false },
        query: { signal: true },
      },
    },
    hooks: {
      afterAllFilesWrite: "pnpm exec biome check --write",
    },
  },
  // Zod schemas for request/response validation (forms, react-hook-form later).
  zod: {
    input: { target: INPUT },
    output: {
      target: "./frontend/src/api/zod",
      client: "zod",
      mode: "tags-split",
      clean: true,
    },
    hooks: {
      afterAllFilesWrite: "pnpm exec biome check --write",
    },
  },
});
