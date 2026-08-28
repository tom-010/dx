import logging
import click
from django_click import command


@command()
@click.argument("logger_name", type=str)
@click.argument("log_level", type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], case_sensitive=False))
def setloglevel(logger_name, log_level):
    """
    Dynamically change the logging level without restarting the server.

    Example usage:
        python manage.py setloglevel django DEBUG
        python manage.py setloglevel "" INFO
    """
    logger = logging.getLogger(logger_name)
    logger.setLevel(getattr(logging, log_level.upper()))
    click.secho(f"Logger '{logger_name}' level set to {
                log_level.upper()}", fg="green")
