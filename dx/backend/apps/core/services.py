"""Business logic behind the sample tasks. Plain typed Python; Celery only sees these via
`tasks.py`, so the same functions are usable synchronously (tests, shell, other services)."""

import time
from collections.abc import Callable

from apps.datasets.models import Dataset

ProgressCallback = Callable[[int, int], None]


def add(a: int, b: int) -> int:
    return a + b


def count_to(n: int, delay: float, on_progress: ProgressCallback | None = None) -> int:
    """Slow loop that reports progress after each step (demo for long-running work)."""
    for current in range(1, n + 1):
        time.sleep(delay)
        if on_progress is not None:
            on_progress(current, n)
    return n


def dataset_summary() -> dict[str, int]:
    return {
        "datasets": Dataset.objects.count(),
        "rows": sum(Dataset.objects.values_list("row_count", flat=True)),
    }


class DemoFailure(Exception):
    """Raised on purpose by the `fail` sample task."""
