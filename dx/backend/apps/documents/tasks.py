"""The extraction task. `snapshot.start_extraction` enqueues it on commit; the body is
`snapshot.run_extraction`, which records a failure on the row rather than raising."""

# Celery inspects task signatures at runtime; keep `Task[P, R]` (celery-types) a string.
from __future__ import annotations

import uuid

from apps.core.tasks import tenant_task
from apps.documents import snapshot


@tenant_task()
def extract_content(owner_id: uuid.UUID, content_id: uuid.UUID | str) -> str:
    """Run one queued extraction; returns the terminal status."""
    content = snapshot.run_extraction(uuid.UUID(str(content_id)))
    return str(content.status)
