---
paths:
  - "**/scripts/check.py"
  - "**/scripts/ci.py"
  - "**/backend/pyproject.toml"
  - "**/frontend/tsconfig*.json"
  - "**/frontend/biome.json"
---

## Type checking (`scripts/check.py`)

`./scripts/check.py [backend|frontend]` runs mypy and `tsc -b`, every step regardless of earlier
failures, and exits 1 if any fails (`./scripts/ci.py` = the same steps + `./scripts/build.sh`,
the production image). Stdlib-only Python (no venv needed for the script; `uv run`
supplies the backend's), sources `scripts/_pnpm.sh` for pnpm. Policy: **strict, plus every
check that catches a logic error — none that polices style.** Null/undefined handling first.
The configuration lives with the tools, not in the script:

- **Backend** (`backend/pyproject.toml [tool.mypy]`): `strict` + django-stubs plugin, plus
  `warn_unreachable` (dead branches, `is None` on a non-optional), `disallow_any_unimported`
  (an untyped import must not silently become `Any` — write a stub under `backend/stubs/`
  instead, as for `djclick` and `storages`) and the error codes `possibly-undefined`,
  `redundant-expr`, `truthy-bool`, `truthy-iterable`, `exhaustive-match`, `deprecated`,
  `unused-awaitable`, `ignore-without-code`. In practice: `# type: ignore[code]` only; `if obj:`
  on a non-optional type is an error (write `is not None`, or the type is missing `| None`);
  the test client's response cannot be `isinstance`-narrowed to `StreamingHttpResponse`
  (`cast` it, see `test_tasks.py`). Signatures are forced by ruff `ANN` (every parameter and
  return annotated, no `Any` in them). There is no `disallow_any_explicit` override any more:
  the logic now sits in `api.py` beside ninja's `Field(...)`, which is typed `Any`. Keep `Any`
  out of your own signatures anyway (`UploadedFile[bytes]`, not `[Any]`).
- **Frontend** (`frontend/tsconfig.app.json` + `tsconfig.node.json`, checked by `tsc -b` — the
  same call `pnpm build` makes): `strict` (the TS 6 default, kept explicit),
  `noUncheckedIndexedAccess` (`xs[0]` and `record[key]` are `T | undefined` — handle it),
  `noImplicitReturns`, `noImplicitOverride`, `noUncheckedSideEffectImports`,
  `allowUnreachableCode: false`, `allowUnusedLabels: false`. Explicit signatures are forced
  by Biome (`biome.json`): the recommended set bans `any`, `!`, untyped `let` and evolving
  types, and `nursery/useExplicitType` requires a return type on every function and a type on
  every parameter that is not inferred from a call argument — components return
  `JSX.Element` (`import type { JSX } from "react"`), handlers `void`, JSX-attribute arrows
  are written `(event: ChangeEvent<HTMLInputElement>): void => …`, object-property callbacks
  (`onSuccess`, `refetchInterval`, `beforeLoad`) type their parameter explicitly, module-level
  consts that are not literals get a type. Callbacks passed straight to a call (`.map(fn)`,
  `setState(fn)`) are exempt. `src/components/ui/**` (vendored shadcn) is excluded so
  `shadcn add` stays frictionless.
- **Deliberately off**: `exactOptionalPropertyTypes` (fights library and orval-generated types —
  `options?: T` vs a form's `T | undefined`), `noPropertyAccessFromIndexSignature` (style, not
  safety), mypy `disallow_any_explicit` (Django needs `Any` at its edges). Formatting and line
  length are ruff/Biome's business (`./scripts/lint.sh`), never the type checkers'.
