import djclick as click
from rich import print
import logging
from core.models import User

logger = logging.getLogger(__name__)


@click.command()
@click.argument("name")
def command(name):
    print("Hello, [bold magenta]{}[/bold magenta]".format(name))
    from core.tasks import slow_count

    result = slow_count.delay(10)
    for i in range(10):
        import time

        User.objects.count()

        time.sleep(0.3)
        print(result.state)
