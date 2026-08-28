"""`manage.py ensure_bucket` — create the media and backup buckets with versioning on.

Idempotent. Runs before `migrate` on a fresh object store: ./scripts/db.sh and
docker/entrypoint.sh do. With MEDIA_STORAGE=local there is nothing to do.
"""

import djclick as click
from rich.console import Console

from apps.core.storage import ensure_bucket, s3_storage

console = Console()

STORAGE_ALIASES = ("default", "backups")


@click.command()
def command() -> None:
    """Create the S3 buckets for uploads and backups (if missing) and enable versioning."""
    for alias in STORAGE_ALIASES:
        storage = s3_storage(alias)
        if storage is None:
            console.print(f"[dim]{alias}: local storage, no bucket to create[/dim]")
            continue
        status = ensure_bucket(storage.connection.meta.client, storage.bucket_name)
        console.print(
            f"[green]✓[/green] {alias}: bucket [bold]{status.bucket}[/bold] at "
            f"{storage.endpoint_url}: {'created' if status.created else 'exists'}, "
            f"versioning {'enabled' if status.versioning_enabled else 'already on'}"
        )
