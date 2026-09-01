import djclick as click
import structlog
from django.conf import settings
from apps.documents import models

from apps.accounts.models import User

console = Console()
log = structlog.get_logger(__name__)


@click.command()
def command() -> None:
    """Greet NAME and show a few facts about this environment."""
    print("hi")

    models.Document()
