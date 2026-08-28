import djclick as click
import sys
from django.contrib.auth import authenticate
from config.auth_api import create_token


@click.command()
@click.option(
    '--username', '-u',
    default='admin',
    help='Username for authentication (default: admin)'
)
@click.option(
    '--password', '-p',
    default='admin',
    help='Password for authentication (default: admin)'
)
def command(username, password):
    """Get a JWT token (outputs only the token for scripting)"""

    # Authenticate user
    user = authenticate(username=username, password=password)
    if not user:
        click.echo('Invalid credentials', err=True)
        sys.exit(1)

    # Generate and output token (just the token, nothing else)
    token = create_token(user)
    click.echo(token, nl=False)