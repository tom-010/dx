#!/usr/bin/env python3
"""Everything CI runs, in one command: the type checks plus the production image build.

    ./scripts/ci.py                 # check.py steps (mypy, tsc -b), then ./scripts/build.sh
    ./scripts/ci.py backend image   # a subset, by step name

Steps come from scripts/check.py (same runner, summary and exit code: 1 if any step failed,
every step runs regardless of earlier failures). The image step runs ./scripts/build.sh, which
`docker build`s docker/Dockerfile into dx-app:latest (override with IMAGE_TAG / APP_VERSION as
for build.sh) — the frontend is built inside the image, the deploy checklist runs when the
container starts. Lint, tests and the generated-client drift check stay in ./scripts/check.sh.
"""

from __future__ import annotations

import shutil
import sys

import check


class ImageStep(check.Step):
    def missing_prerequisite(self) -> str | None:
        if shutil.which("docker") is None:
            return "docker is not installed (build.sh needs `docker build`)"
        return None


STEPS: tuple[check.Step, ...] = (
    *check.STEPS,
    ImageStep(
        name="image",
        tool="build.sh",
        config="docker/Dockerfile -> dx-app:latest",
        cwd=check.ROOT,
        command=(str(check.ROOT / "scripts" / "build.sh"),),
    ),
)


if __name__ == "__main__":
    sys.exit(
        check.main(
            steps=STEPS,
            description="Type-check the backend and frontend, then build the production image.",
        )
    )
