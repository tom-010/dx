"""The extraction task. `snapshot.start_extraction` enqueues it on commit, under the
snapshot's own id, and the body is `snapshot.run_extraction` — which records a failure on the
row rather than raising.

Progress: what the strategy reports (`snapshot.report_progress`) becomes Celery's `PROGRESS`
state, so `GET /api/tasks/{id}` and the SSE stream behind `ExtractionOut.stream_url` show a
page count while a long OCR run works, without the row being written to once per page.
"""

# Celery inspects task signatures at runtime; keep `Task[P, R]` (celery-types) a string.
from __future__ import annotations

import uuid

from celery import current_task

from apps.core.tasks import PROGRESS, tenant_task
from apps.documents import snapshot


@tenant_task()
def extract_content(owner_id: uuid.UUID, content_id: uuid.UUID | str) -> str:
    """Run one queued extraction; returns the terminal status."""

    def report(current: int, total: int) -> None:
        # Eager mode (tests) has no worker to report to; the caller gets the result anyway.
        if not current_task.request.is_eager:
            current_task.update_state(state=PROGRESS, meta={"current": current, "total": total})

    content = snapshot.run_extraction(uuid.UUID(str(content_id)), progress=report)
    return str(content.status)
