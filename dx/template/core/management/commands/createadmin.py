import djclick as click
from django.contrib.auth import get_user_model
from rich import print


@click.command()
@click.option("--username", "-u", default="admin")
@click.option("--email", "-e")
def command(username, email):
    email = email or f"{username}@example.com"
    User = get_user_model()
    existing = User.objects.filter(username=username)
    if existing:
        print(f"User {username} already exists")
        return
    User.objects.create_superuser(username, email, username)
