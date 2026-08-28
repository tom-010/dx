import djclick as click
from core.models import User
from persons.models import Person
from rich import print


@click.command()
@click.argument("username")
def command(username):

    user = User.objects.filter(username=username).first()
    if not user:
        user = User.objects.create(
            username=username,
        )

    person = Person.objects.filter(user=user).first()
    if not person:
        person = Person.objects.create(
            name=username,
            user=user,
        )

    print(f"Created user {username}")
