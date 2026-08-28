#!/usr/bin/env python3
"""Type-check the repository: mypy for the backend, tsc for the frontend.

    ./scripts/check.py             # both (the default)
    ./scripts/check.py backend     # mypy only
    ./scripts/check.py frontend    # tsc only

Every step runs even if an earlier one fails; the summary lists them all and the exit code is 1
if any failed. Type checking only — no formatting or style rules (those are ./scripts/lint.sh).
./scripts/ci.py runs these steps plus the production image build. The configuration lives with
the tools, not here:

- backend:  backend/pyproject.toml [tool.mypy] — strict + django-stubs plugin + the logic checks
            (warn_unreachable, disallow_any_unimported, possibly-undefined, ...).
- frontend: frontend/tsconfig.app.json + tsconfig.node.json — strict, noUncheckedIndexedAccess,
            noImplicitReturns, ... (`tsc -b` checks both projects, the same call `pnpm build` makes).

Standard library only, so it runs with any Python >= 3.10 — no venv needed for the script itself
(`uv run` provides the backend's). pnpm is installed via nvm, which only loads in interactive
shells; the frontend step sources scripts/_pnpm.sh exactly like the other scripts do.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PNPM_ENV = ROOT / "scripts" / "_pnpm.sh"


@dataclass(frozen=True)
class Step:
    name: str
    tool: str
    config: str
    cwd: Path
    command: Sequence[str]

    def missing_prerequisite(self) -> str | None:
        """A human-readable reason the step cannot run, or None."""
        return None


class BackendStep(Step):
    def missing_prerequisite(self) -> str | None:
        if shutil.which("uv") is None:
            return "uv is not installed (https://docs.astral.sh/uv/); it manages backend/.venv"
        return None


class FrontendStep(Step):
    def missing_prerequisite(self) -> str | None:
        if not (self.cwd / "node_modules" / ".bin" / "tsc").exists():
            return "frontend/node_modules is missing: run `pnpm install` in frontend/"
        if not (self.cwd / "src" / "routeTree.gen.ts").exists():
            return (
                "frontend/src/routeTree.gen.ts is missing: run `pnpm exec vite build` once in "
                "frontend/ (the router plugin generates it)"
            )
        return None


STEPS: tuple[Step, ...] = (
    BackendStep(
        name="backend",
        tool="mypy",
        config="backend/pyproject.toml [tool.mypy]",
        cwd=ROOT / "backend",
        command=("uv", "run", "mypy", "."),
    ),
    FrontendStep(
        name="frontend",
        tool="tsc -b",
        config="frontend/tsconfig.app.json + tsconfig.node.json",
        cwd=ROOT / "frontend",
        # `. _pnpm.sh` puts nvm's pnpm on PATH when the shell is not interactive.
        command=("bash", "-c", f'. "{PNPM_ENV}" && exec pnpm exec tsc -b'),
    ),
)


@dataclass(frozen=True)
class Result:
    step: Step
    exit_code: int
    seconds: float
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def use_color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def paint(text: str, code: str) -> str:
    return f"\x1b[{code}m{text}\x1b[0m" if use_color() else text


def run(step: Step) -> Result:
    print(paint(f"== {step.name}: {step.tool}", "1"), paint(f"({step.config})", "2"), flush=True)
    reason = step.missing_prerequisite()
    if reason is not None:
        print(paint(f"skipped: {reason}", "31"), flush=True)
        return Result(step, exit_code=1, seconds=0.0, note=reason)
    started = time.perf_counter()
    completed = subprocess.run(step.command, cwd=step.cwd)
    return Result(step, exit_code=completed.returncode, seconds=time.perf_counter() - started)


def summarize(results: Sequence[Result]) -> None:
    print()
    print(paint("== summary", "1"))
    width = max(len(r.step.name) for r in results)
    for r in results:
        mark = paint("ok  ", "32") if r.ok else paint("FAIL", "31")
        line = f"  {mark}  {r.step.name:<{width}}  {r.step.tool:<7} {r.seconds:5.1f}s"
        if not r.ok:
            line += f"  (exit {r.exit_code})" if not r.note else f"  ({r.note})"
        print(line)


def main(
    argv: Sequence[str] | None = None,
    steps: Sequence[Step] = STEPS,
    description: str = "Type-check the backend (mypy) and the frontend (tsc).",
) -> int:
    """Run `steps` (a subset can be picked on the command line); ci.py reuses this."""
    parser = argparse.ArgumentParser(
        description=description,
        epilog="Exit code 1 if any step fails; every step runs regardless of earlier failures.",
    )
    parser.add_argument(
        "targets",
        nargs="*",
        choices=[s.name for s in steps],
        metavar="target",
        help="which steps to run: %(choices)s (default: all)",
    )
    args = parser.parse_args(argv)
    selected = [s for s in steps if not args.targets or s.name in args.targets]
    results = [run(step) for step in selected]
    summarize(results)
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
