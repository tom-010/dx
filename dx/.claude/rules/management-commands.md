---
paths:
  - "**/management/commands/**"
  - "**/backend/stubs/**"
---

## Management commands (django-click + rich)

- Commands are click commands (`import djclick as click`, the function is named `command`,
  `@click.command()` + `@click.argument`/`@click.option`), not `BaseCommand` subclasses: typed,
  validated options, `--help`, prompts and confirmations for free; Django's `--settings`,
  `--pythonpath`, `--traceback`, `-v` still work. Output through rich (`Console`, `Table`,
  `Panel`, progress bars); `click.echo(..., nl=False)` only for bare values scripts capture
  (`token`). User-facing failures: `raise click.ClickException("…")` (exit 1, red); wrong usage:
  `click.UsageError` (exit 2).
- **`manage.py hello_world [NAME] [--shout] [-n N]`** is the reference implementation (argument,
  options, rich panel + table, structured log line) — copy it for new commands. Logic beyond
  parsing and printing goes to a service module; long work is a Celery task.
- **Every app has `management/commands/`** ready (two empty `__init__.py`s), and `newapp`
  scaffolds them for new apps — a command lives in the app it belongs to, not in `core` by
  default. `test_commands.py::test_every_app_can_hold_commands` fails on an app that lost them.
- **`manage.py newcommand NAME [--app APP]`** writes one from `backend/scaffold/command.py.tmpl`
  (asking which app when `--app` is left out) — the shape below, already correct. Rendering is
  `apps/core/scaffold.py::render_command`.
- **Commands are not tenant-filtered.** A command is a database task, not a request for one
  user, so the body goes in `with all_tenants():` (`apps/core/db.py`) — the ORM scope raises
  `ScopeError` without a tenant rather than filtering, and that is only in the way here. The
  template writes that line for you. **It lifts the ORM scope, not row-level security**: reading
  owned rows still needs a connection the policies do not apply to (`DB_ROLE=migrator manage.py
  …`), so a command that reads them says `rls.require_cross_tenant_access()` and fails with that
  instruction instead of silently finding nothing. A command that acts *for* one user pins them
  instead (`pin_session_tenant(user.pk)`, as `shell_as` and `load_tenant` do).
- **Every invocation is recorded** in `core.CommandRun` (`apps/core/usage.py`): the command name
  and its arguments as one string. The hook is in `manage.py` itself — the one place they all
  pass through, so a new command is covered the day it is written and none of them logs itself.
  `call_command()` from code and the test suite deliberately does not land there (this is a
  record of what a *person* ran), `tui` is excluded (it reads the log), and `record_run`
  swallows every database error: the first `migrate` runs before that table exists.
- **`manage.py tui [WORDS…] [-d] [--list]`** is how you find one: a Textual app listing every
  command (ours, Django's, the libraries') grouped by the app that ships it, fuzzy on the name,
  with the highlighted command's real `--help` beside it. **The one input is both the search box
  and the command line**: enter picks the highlighted command into it, you type the arguments
  after the name (`newapp reports --model Report`), the bottom line shows the exact line, and
  enter **leaves the explorer and runs that line**: the app only returns it, so the command has
  the terminal to itself, its output is the last thing on screen and its exit code becomes the
  process's. (It runs as a child process — inside the app it would hit Django's
  `SynchronousOnlyOperation`, since Textual is an asyncio loop.) Every argument goes into the
  filter box, so `manage.py tui delete tenant` opens on that query. Search is fzf-style: every
  term has to match as a subsequence, in any order ("del ten" → `delete_tenant`); an unfiltered
  list leads with the commands you ran last (`usage.recent_runs`), and recency breaks ties while
  filtering. ctrl+f (`-d`) widens the search to the descriptions;
  ctrl+p is textual's palette (themes). It opens in the `ansi-dark` theme, so the terminal's own
  colours and background show through. A pipe or `--list` gets a plain table instead. The index,
  ranking, grouping and captured help are `apps/core/cli.py` (`command_index`, `fuzzy_score`,
  `search`, `group_by_app`, `full_help`) and are what the tests drive; the command module is the
  screen. A command whose module does not import is listed in red with the import error — that
  is the point of a tool for looking at commands.
- Existing: `createadmin`, `token` (accounts); `ensure_bucket`, `backup`, `restore`,
  `newapp`, `deleteapp`, `newcommand`, `hello_world`, `playground` (a click group of
  experiments: `gemini` via `GEMINI_API_KEY`, `render` = the sample PDF rasterized page by page to PNGs), `tui`, `rls_sync`, `pull_tenant`,
  `load_tenant`, `delete_tenant` (core; the last three are one user's data — `docs/tenant-data.md`). Exception to
  `extract` (documents; runs any extraction strategy on a file — **stdout only, nothing
  written**, `--strategy` or a numbered picker, `-f text|html|json|outline`). Exception to
  the click rule: `shell_as` and `shell_admin` subclass Django's `shell` command (`BaseCommand`)
  so the REPL runs in-process inside the pinned tenant context; test them with `call_command`.
- Tests use `click.testing.CliRunner().invoke(module.command, [...])` and assert on
  `result.exit_code` / `result.output` (`call_command` does not understand click options).
- django-click ships no type hints: the mini-stub `backend/stubs/djclick/__init__.pyi`
  (`mypy_path = "stubs"`) re-exports click's types. Add stubs there for other untyped packages
  instead of `ignore_missing_imports`.
