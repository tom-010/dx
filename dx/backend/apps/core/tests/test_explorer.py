"""The lineage explorer (`apps/core/explorer.py`): a staff, development-only browser over every
model, its versions and its lineage.

What is worth testing about a read-only dev tool is not its HTML but the three things that would
make it a liability: that it does not exist outside development, that it is not open to
non-staff, and that browsing a tenant shows *that* tenant's rows and nobody else's.
"""

import re
import uuid
from datetime import timedelta

import pytest
from django.db.models import Model
from django.test import Client
from django.urls import reverse
from django.utils import timezone
from pytest_django.fixtures import Settings

from apps.accounts.models import User
from apps.core import explorer, lineage
from apps.core.history import event_model_for, event_rows, history_context
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for
from apps.datasets.models import Dataset

pytestmark = pytest.mark.django_db


@pytest.fixture
def staff_client(staff_user: User) -> Client:
    """The explorer authenticates by session, like the admin — not by bearer token."""
    client = Client()
    client.force_login(staff_user)
    return client


def users_url() -> str:
    return reverse("explorer:users")


def index_url(user: User) -> str:
    return reverse("explorer:index", args=[user.pk])


def model_url(user: User, model: type[Model]) -> str:
    meta = model._meta
    return reverse("explorer:model", args=[user.pk, meta.app_label, meta.model_name])


def object_url(user: User, obj: Model) -> str:
    meta = type(obj)._meta
    return reverse("explorer:object", args=[user.pk, meta.app_label, meta.model_name, obj.pk])


# --- the guards ---------------------------------------------------------------------------------


def test_every_page_is_gone_outside_development(
    staff_client: Client, staff_user: User, settings: Settings
) -> None:
    """The second of the two guards: with the paths mounted, every view still refuses.

    The URLs are resolved *before* the setting is flipped on purpose. `config/urls.py` reads
    `EXPLORER_ENABLED` once, when Django first imports it — so a test that turned the flag off
    first would merely be asserting that an unmounted URL 404s, and would leave the explorer
    missing from the URLconf for every test after it.
    """
    urls = [users_url(), index_url(staff_user)]

    settings.EXPLORER_ENABLED = False

    for url in urls:
        assert staff_client.get(url).status_code == 404


def test_a_non_staff_user_is_refused(client: Client, user: User) -> None:
    client.force_login(user)

    assert client.get(users_url()).status_code == 403


def test_anonymous_is_sent_to_the_login_page(client: Client) -> None:
    response = client.get(users_url())

    assert response.status_code == 302
    assert "/login/" in response.headers["Location"]


# --- the pages ----------------------------------------------------------------------------------


def test_the_first_page_lists_the_users_to_look_at(
    staff_client: Client, staff_user: User, user: User
) -> None:
    """Tenant == user, so picking one is the root of the hierarchy, not a filter on it."""
    response = staff_client.get(users_url())
    body = response.content.decode()

    assert response.status_code == 200
    assert user.get_username() in body
    assert index_url(user) in body
    assert index_url(staff_user) in body


def test_the_model_list_counts_one_tenant_s_rows(
    staff_client: Client, user: User, other_user: User
) -> None:
    """The count is what that user owns — the whole point of picking a user first."""
    with acting_as(user):
        create_dataset_for(user, name="alice's")
    with acting_as(other_user):
        create_dataset_for(other_user, name="bob's")
        create_dataset_for(other_user, name="bob's second")

    mine = staff_client.get(index_url(user)).content.decode()
    theirs = staff_client.get(index_url(other_user)).content.decode()

    assert f'<a href="{model_url(user, Dataset)}">Dataset</a>' in mine
    # The row of the Dataset table, with its count, for each tenant in turn.
    assert _dataset_count(mine) == 1
    assert _dataset_count(theirs) == 2


def _dataset_count(body: str) -> int:
    """The count in the Dataset row of the model table — the last numeric cell of that row."""
    row = next((chunk for chunk in body.split("<tr>") if ">Dataset</a>" in chunk), None)
    assert row is not None, "no Dataset row in the model list"
    return int(re.findall(r"<td>(\d+)</td>", row)[-1])


def test_rows_of_one_model_link_to_the_object_page(staff_client: Client, user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="the one")

    response = staff_client.get(model_url(user, Dataset))
    body = response.content.decode()

    assert response.status_code == 200
    assert "the one" in body
    assert object_url(user, dataset) in body


def test_one_tenant_never_sees_another_s_rows(
    staff_client: Client, user: User, other_user: User
) -> None:
    """The isolation contract, through this tool: browsing as A must not reach B's row even
    though the staff user could pick B on the previous page."""
    with acting_as(other_user):
        theirs = create_dataset_for(other_user, name="bob's secret")

    listing = staff_client.get(model_url(user, Dataset)).content.decode()
    assert "bob's secret" not in listing

    # ...and the object page for B's row, opened in A's context, is a 404 rather than a peek.
    url = reverse("explorer:object", args=[user.pk, "datasets", "dataset", theirs.pk])
    assert staff_client.get(url).status_code == 404


def test_a_soft_deleted_row_is_hidden_but_never_silently(staff_client: Client, user: User) -> None:
    """The default matches the app's own (`Model.objects` hides them), because a table is mostly
    retired rows soon enough. What must not happen is hiding them *quietly*: the caption counts
    what was left out and links to it."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="a retired row")
        dataset.soft_delete()

    default = staff_client.get(model_url(user, Dataset)).content.decode()
    everything = staff_client.get(f"{model_url(user, Dataset)}?state=all").content.decode()

    assert "a retired row" not in default
    assert "1 retired hidden" in default
    assert "a retired row" in everything
    assert "deleted" in everything


def test_the_object_page_shows_the_version_history(staff_client: Client, user: User) -> None:
    with acting_as(user), history_context("test"):
        dataset = create_dataset_for(user, name="first name")
        dataset.name = "second name"
        dataset.save(operation=None, sources=[])

    body = staff_client.get(object_url(user, dataset)).content.decode()

    assert "v1" in body
    assert "v2" in body
    assert "first name" in body  # the old value, as a diff
    assert "second name" in body


def test_the_object_page_shows_both_directions_of_the_lineage(
    staff_client: Client, user: User
) -> None:
    """The two questions the tool exists for: what was this built from, and what came out of it —
    each naming the *version* consumed, plus the code that recorded the edge."""
    with acting_as(user):
        with history_context("the-run"):
            source = create_dataset_for(user, name="the source")
            derived = create_dataset_for(user, name="the derived")
            lineage.record_derivation(derived, sources=[source])
        source.name = "the source, renamed"
        source.save(operation=None, sources=[])

    downstream = staff_client.get(object_url(user, source)).content.decode()
    upstream = staff_client.get(object_url(user, derived)).content.decode()

    # From the source: what was built out of it.
    assert "the derived" in downstream
    # From the derived row: what it consumed, at the version it consumed — and that the source
    # has moved on since, which is the whole reason edges point at versions.
    assert "the source" in upstream
    assert "superseded" in upstream  # the source has moved on since it was consumed
    assert "the-run" in upstream
    assert "record_derivation" in upstream  # Lineage.stack: the line that claimed the edge


def test_an_untracked_model_says_so_instead_of_breaking(staff_client: Client, user: User) -> None:
    """`Lineage` itself is exempt from history: it is the graph, not a subject of it."""
    with acting_as(user):
        source = create_dataset_for(user, name="source")
        derived = create_dataset_for(user, name="derived")
        edge = lineage.record_derivation(derived, sources=[source])[0]

    response = staff_client.get(
        reverse("explorer:object", args=[user.pk, "core", "lineage", edge.pk])
    )

    assert response.status_code == 200
    assert "Not versioned" in response.content.decode()


def test_an_unknown_model_or_row_is_a_404(staff_client: Client, user: User) -> None:
    unknown_model = reverse("explorer:model", args=[user.pk, "datasets", "nope"])
    missing_row = reverse("explorer:object", args=[user.pk, "datasets", "dataset", uuid.uuid4()])
    unknown_tenant = reverse("explorer:index", args=[uuid.uuid4()])

    for url in (unknown_model, missing_row, unknown_tenant):
        assert staff_client.get(url).status_code == 404


# --- the registry the pages are built from ------------------------------------------------------


def test_the_model_list_is_derived_from_the_registry() -> None:
    """A new app must appear without anyone editing this tool — and third-party tables must not.

    `django.contrib.auth.Permission` is the canary: installed, full of rows, and none of this
    project's business.
    """
    labels = {model._meta.label for model in explorer.explorer_models()}

    assert "datasets.Dataset" in labels
    assert "datasets.DatasetEvent" in labels  # history tables are worth browsing too
    assert "core.Lineage" in labels
    assert "accounts.User" in labels
    assert not any(label.startswith(("auth.", "admin.", "contenttypes.")) for label in labels)


def test_every_model_is_labelled_as_what_it_is() -> None:
    event_model = event_model_for(Dataset)
    assert event_model is not None

    assert explorer.kind_of(Dataset) == "owned"
    assert explorer.kind_of(User) == "shared"
    assert explorer.kind_of(event_model) == "history"


def test_history_tables_are_ordered_by_when_the_version_was_written() -> None:
    """An event table mirrors the tracked row's `created`, so ordering by that would sort every
    version of one row into a single clump at the object's original position."""
    event_model = event_model_for(Dataset)
    assert event_model is not None

    assert explorer._order_by(event_model) == "-pgh_created_at"
    assert explorer._order_by(Dataset) == "-created"


# --- paging and the date filter -----------------------------------------------------------------


def test_the_row_list_pages(staff_client: Client, user: User) -> None:
    """`PAGE_SIZE` rows a page, newest first, and the pages do not overlap."""
    with acting_as(user):
        for index in range(explorer.PAGE_SIZE + 3):
            create_dataset_for(user, name=f"dataset {index:02d}")

    first = staff_client.get(model_url(user, Dataset)).content.decode()
    second = staff_client.get(f"{model_url(user, Dataset)}?page=2").content.decode()

    assert _row_ids(first) and _row_ids(second)
    assert len(_row_ids(first)) == explorer.PAGE_SIZE
    assert len(_row_ids(second)) == 3
    assert not set(_row_ids(first)) & set(_row_ids(second))
    assert "page 1 of 2" in first


def test_a_nonsense_page_number_is_clamped_not_a_crash(staff_client: Client, user: User) -> None:
    """These links come from stale bookmarks, not from typing; a 404 helps nobody."""
    with acting_as(user):
        create_dataset_for(user, name="only one")

    for page in ("0", "999", "not-a-number"):
        response = staff_client.get(f"{model_url(user, Dataset)}?page={page}")
        assert response.status_code == 200
        assert "only one" in response.content.decode()


def test_the_date_filter_narrows_to_whole_days(staff_client: Client, user: User) -> None:
    """Both bounds are inclusive days: a row written this afternoon must still be inside a range
    that ends today, which is why the upper bound becomes `< tomorrow`."""
    with acting_as(user):
        create_dataset_for(user, name="written today")
    today = timezone.localdate().isoformat()
    long_ago = (timezone.localdate() - timedelta(days=30)).isoformat()

    inside = staff_client.get(f"{model_url(user, Dataset)}?on=created&from={today}&to={today}")
    before = staff_client.get(f"{model_url(user, Dataset)}?on=created&to={long_ago}")

    assert "written today" in inside.content.decode()
    assert "written today" not in before.content.decode()
    assert "No rows in that date range." in before.content.decode()


def test_paging_keeps_the_filter(staff_client: Client, user: User) -> None:
    """A "next page" that quietly drops the filter is a list lying about what it shows."""
    with acting_as(user):
        for index in range(explorer.PAGE_SIZE + 1):
            create_dataset_for(user, name=f"dataset {index:02d}")
    today = timezone.localdate().isoformat()

    body = staff_client.get(f"{model_url(user, Dataset)}?on=created&from={today}").content.decode()
    next_url = re.search(r'href="([^"]*)" rel="next"', body)

    assert next_url is not None
    assert "on=created" in next_url.group(1)
    assert f"from={today}" in next_url.group(1)


def test_a_malformed_date_is_reported_and_ignored(staff_client: Client, user: User) -> None:
    """Half a range is still a useful answer, and a 500 for a typo in a URL is not."""
    with acting_as(user):
        create_dataset_for(user, name="still here")

    url = f"{model_url(user, Dataset)}?on=created&from=yesterday"
    body = staff_client.get(url).content.decode()

    assert "expected a date like" in body
    assert "still here" in body


def test_each_table_is_filtered_on_a_timestamp_it_actually_has() -> None:
    """`pgh_created_at` leads for an event table — it mirrors the tracked row's `created` too,
    and filtering versions by when the *object* was created is not the question. `Lineage` is
    append-only and has no `modified` at all, so it must not be offered one."""
    event_model = event_model_for(Dataset)
    assert event_model is not None

    assert explorer.timestamp_fields(Dataset) == ["created", "modified"]
    assert explorer.timestamp_fields(event_model)[0] == "pgh_created_at"
    assert explorer.timestamp_fields(lineage.Lineage) == ["created"]


def _row_ids(body: str) -> list[str]:
    """The ids of the listed rows, in order.

    Read out of the table body, not the whole page: the ids come from the row links now that the
    table no longer prints them as a column, and the breadcrumb link to the tenant is a uuid too.
    """
    table = body.split("<tbody>", 1)[-1].split("</tbody>", 1)[0]
    return re.findall(r'href="[^"]*/([0-9a-f-]{36})/"', table)


# --- objects versus versions ---------------------------------------------------------------------


def version_url(user: User, obj: Model, version: int) -> str:
    meta = type(obj)._meta
    return reverse(
        "explorer:version", args=[user.pk, meta.app_label, meta.model_name, obj.pk, version]
    )


def test_the_version_page_shows_the_state_as_it_was(staff_client: Client, user: User) -> None:
    """Historical values, not current ones — the point of having a separate page at all."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="as first written")
        dataset.name = "as it is now"
        dataset.save(operation=None, sources=[])

    first = staff_client.get(version_url(user, dataset, 1)).content.decode()
    second = staff_client.get(version_url(user, dataset, 2)).content.decode()

    assert "as first written" in first
    assert "version 1 of 2" in first
    assert "Superseded" in first  # the row has moved on
    assert "as it is now" in second
    assert "version 2 of 2" in second
    assert "current state" in second


def test_the_two_pages_say_which_one_you_are_on(staff_client: Client, user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="a dataset")

    on_object = staff_client.get(object_url(user, dataset)).content.decode()
    on_version = staff_client.get(version_url(user, dataset, 1)).content.decode()

    assert '<span class="tag">object</span>' in on_object
    assert "<strong>version</strong>" in on_version


def test_every_lineage_link_goes_to_a_version(staff_client: Client, user: User) -> None:
    """An edge names the state that was consumed. Linking to the live row would answer a
    different question — and after a rename, a visibly wrong one."""
    with acting_as(user), history_context("run"):
        source = create_dataset_for(user, name="the source")
        derived = create_dataset_for(user, name="the derived")
        lineage.record_derivation(derived, sources=[source])

    body = staff_client.get(object_url(user, derived)).content.decode()
    links = re.findall(r'href="([^"]+)"', body.split("Derived from")[1])
    to_rows = [link for link in links if "/edge/" not in link]

    assert to_rows
    assert all(re.search(r"/v\d+/$", link) for link in to_rows), to_rows
    assert version_url(user, source, 1) in to_rows
    # The one link that is not a row is the "Recorded by" cell, which opens the call stack.
    assert any("/edge/" in link for link in links)


def test_the_version_page_lineage_is_that_version_alone(staff_client: Client, user: User) -> None:
    """The object page spans every version; this one is the edges of a single state."""
    with acting_as(user):
        source = create_dataset_for(user, name="rates")
        report = create_dataset_for(user, name="report")
        lineage.record_derivation(report, sources=[source])
        source.name = "rates (revised)"
        source.save(operation=None, sources=[])
        report.name = "report (rebuilt)"
        report.save(operation=None, sources=[])
        lineage.record_derivation(report, sources=[source])

    first = staff_client.get(version_url(user, report, 1)).content.decode()
    second = staff_client.get(version_url(user, report, 2)).content.decode()
    whole = staff_client.get(object_url(user, report)).content.decode()

    assert version_url(user, source, 1) in first
    assert version_url(user, source, 2) not in first
    assert version_url(user, source, 2) in second
    assert version_url(user, source, 1) not in second
    # The object page carries both, because "what was this ever built from" spans the versions.
    assert version_url(user, source, 1) in whole
    assert version_url(user, source, 2) in whole


def test_an_event_row_is_shown_as_a_version_not_as_an_object(
    staff_client: Client, user: User
) -> None:
    """An event row *is* a version. Its own object page would present mirrored columns as if
    they were current, so it redirects to the page that knows they are historical."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="tracked")
    event_model = event_model_for(Dataset)
    assert event_model is not None
    with acting_as(user):
        # `event_rows` rather than a filter: `pgh_obj_id` exists only on the generated subclass,
        # so a literal keyword cannot be type-checked against the abstract base.
        event = event_rows(Dataset, dataset.pk).first()
    assert event is not None

    listing = staff_client.get(model_url(user, event_model)).content.decode()
    response = staff_client.get(object_url(user, event))

    assert "Versions</strong> of datasets.Dataset" in listing
    assert version_url(user, dataset, 1) in listing
    assert response.status_code == 302
    assert response.headers["Location"] == version_url(user, dataset, 1)


def test_a_version_that_does_not_exist_is_a_404(staff_client: Client, user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="only v1")
        edge_target = create_dataset_for(user, name="second")
        edge = lineage.record_derivation(edge_target, sources=[dataset])[0]

    assert staff_client.get(version_url(user, dataset, 7)).status_code == 404
    # ...and an unversioned table has no version pages at all.
    untracked = reverse("explorer:version", args=[user.pk, "core", "lineage", edge.pk, 1])
    assert staff_client.get(untracked).status_code == 404


def test_no_page_leaks_a_template_comment(staff_client: Client, user: User) -> None:
    """`{# … #}` is single-line only in Django: a multi-line one is not a comment, it is text,
    and it renders. It did — above the doctype, on every page.

    Every page of the explorer is checked, not just the one that broke, because the mistake is
    invisible in a template and none of the content assertions elsewhere would notice it.
    """
    with acting_as(user), history_context("run"):
        source = create_dataset_for(user, name="a source")
        derived = create_dataset_for(user, name="a derived row")
        lineage.record_derivation(derived, sources=[source])

    pages = [
        users_url(),
        index_url(user),
        model_url(user, Dataset),
        object_url(user, derived),
        version_url(user, derived, 1),
    ]
    for url in pages:
        body = staff_client.get(url).content.decode()
        assert "{#" not in body and "{%" not in body, url
        assert body.lstrip().startswith("<!doctype html>"), url


# --- quick ranges and jump-to-id ------------------------------------------------------------------


def test_the_quick_ranges_filter_and_mark_themselves(staff_client: Client, user: User) -> None:
    """One click each, and the one in force says so — `aria-pressed`, since these are buttons
    that change what the page shows rather than links to somewhere else."""
    with acting_as(user):
        create_dataset_for(user, name="written just now")

    for key in ("today", "24h", "week"):
        body = staff_client.get(f"{model_url(user, Dataset)}?range={key}").content.decode()

        assert "written just now" in body
        assert f'value="{key}"' in body
        assert 'aria-pressed="true"' in body


def test_24_hours_is_a_rolling_window_and_today_is_a_calendar_day(
    staff_client: Client, user: User
) -> None:
    """Different questions, so different answers: a run at 09:00 asking for the last 24 hours
    must not be shown yesterday morning as well, and "today" must start at midnight."""
    with acting_as(user):
        old = create_dataset_for(user, name="two days ago")
        recent = create_dataset_for(user, name="two hours ago")
        # `created` is a database default and must never be assigned in Python, so an aged
        # row can only be made with a queryset update — the one write path round `save()`.
        aged = timezone.now() - timedelta(days=2)
        Dataset.all_objects.filter(pk=old.pk).update(created=aged)
        Dataset.all_objects.filter(pk=recent.pk).update(created=timezone.now() - timedelta(hours=2))

    day = staff_client.get(f"{model_url(user, Dataset)}?range=24h").content.decode()
    today = staff_client.get(f"{model_url(user, Dataset)}?range=today").content.decode()

    assert "two hours ago" in day
    assert "two days ago" not in day
    assert "two days ago" not in today


def test_a_range_wins_over_the_date_inputs(staff_client: Client, user: User) -> None:
    """Both can arrive together — the buttons submit the form they sit in. One rule, stated
    once: the range wins, and the inputs come back empty so the page is not lying about which
    filter produced it."""
    with acting_as(user):
        create_dataset_for(user, name="a row from today")
    long_ago = (timezone.localdate() - timedelta(days=90)).isoformat()

    url = f"{model_url(user, Dataset)}?range=today&from={long_ago}&to={long_ago}"
    body = staff_client.get(url).content.decode()

    assert "a row from today" in body
    # The date inputs come back empty, so the form is not claiming to be what filtered the page.
    assert 'name="from" value=""' in body
    assert 'name="to" value=""' in body


def test_jumping_to_an_id_finds_the_row_without_being_told_the_model(
    staff_client: Client, user: User
) -> None:
    """Ids are UUIDv7 and unique across every table, so one field is enough."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="somewhere in the database")

    response = staff_client.get(reverse("explorer:jump", args=[user.pk]) + f"?id={dataset.pk}")

    assert response.status_code == 302
    assert response.headers["Location"] == object_url(user, dataset)


def test_jumping_from_the_users_page_finds_the_tenant_too(
    staff_client: Client, user: User, other_user: User
) -> None:
    """No tenant in the URL there, and a row is only visible from inside its owner's context —
    so finding the row *is* finding out whose it is."""
    with acting_as(other_user):
        theirs = create_dataset_for(other_user, name="bob's row")

    response = staff_client.get(reverse("explorer:find") + f"?id={theirs.pk}")

    assert response.status_code == 302
    assert response.headers["Location"] == object_url(other_user, theirs)


def test_jumping_to_a_version_row_lands_on_the_version(staff_client: Client, user: User) -> None:
    """An event row's primary key is its `pgh_id`; pasting one lands on the version it names."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="tracked")
        event = event_rows(Dataset, dataset.pk).first()
    assert event is not None

    response = staff_client.get(reverse("explorer:find") + f"?id={event.pk}")

    assert response.headers["Location"] == version_url(user, dataset, 1)


def test_an_id_that_is_nowhere_comes_back_where_it_was_typed(
    staff_client: Client, user: User
) -> None:
    with acting_as(user):
        create_dataset_for(user, name="something")
    listing = model_url(user, Dataset)

    for wanted in (str(uuid.uuid4()), "not-a-uuid"):
        response = staff_client.get(
            reverse("explorer:jump", args=[user.pk]) + f"?id={wanted}&back={listing}"
        )
        assert response.status_code == 302
        assert response.headers["Location"].startswith(listing)
        assert "missing=" in response.headers["Location"]

    assert "Nothing found with id" in staff_client.get(f"{listing}?missing=nope").content.decode()


def test_the_return_address_cannot_point_off_the_explorer(staff_client: Client, user: User) -> None:
    """A "where did you come from" parameter that accepts anything is an open redirect."""
    url = reverse("explorer:jump", args=[user.pk])

    response = staff_client.get(f"{url}?id=nope&back=https://example.com/phish")

    assert response.headers["Location"].startswith(index_url(user))


# --- the edge page --------------------------------------------------------------------------------


def test_recorded_by_opens_the_whole_call_stack(staff_client: Client, user: User) -> None:
    """A listing has room for the innermost frame, which says *where* but not how it got there:
    a derivation recorded from a task, a command and a request looks identical at the bottom of
    the stack and completely different a few frames up."""
    with acting_as(user), history_context("the-run"):
        source = create_dataset_for(user, name="a source")
        derived = create_dataset_for(user, name="a derived row")
        edge = lineage.record_derivation(derived, sources=[source])[0]

    listing = staff_client.get(object_url(user, derived)).content.decode()
    edge_page = reverse("explorer:edge", args=[user.pk, edge.pk])
    body = staff_client.get(edge_page).content.decode()

    assert edge_page in listing  # the "Recorded by" cell links to it
    assert "the-run" in body
    assert "frame" in body
    # Both ends of the edge, each at the version it names, and the build that recorded it.
    assert version_url(user, source, 1) in body
    assert version_url(user, derived, 1) in body
    assert "record_derivation" in body


def test_the_stack_tells_this_project_apart_from_its_dependencies(
    staff_client: Client, user: User
) -> None:
    """Most of a stack is framework plumbing; the two or three frames that matter are the app's,
    and paths inside the repository are shown relative to it."""
    with acting_as(user):
        source = create_dataset_for(user, name="source")
        derived = create_dataset_for(user, name="derived")
        edge = lineage.record_derivation(derived, sources=[source])[0]

    frames = explorer._frames(edge.stack)

    assert frames[0].depth == 1  # outermost first
    ours = [frame for frame in frames if frame.ours]
    assert ours, [frame.location for frame in frames]
    assert all(not frame.location.startswith("/") for frame in ours)
    assert any(".venv" in frame.location for frame in frames)


def test_an_unknown_edge_is_a_404(staff_client: Client, user: User) -> None:
    missing = reverse("explorer:edge", args=[user.pk, uuid.uuid4()])

    assert staff_client.get(missing).status_code == 404


# --- soft delete, on every surface that can show it -----------------------------------------------


def test_a_deleted_row_says_so_at_the_top_of_its_page(staff_client: Client, user: User) -> None:
    """It was only in the field list before, which reads as alive at a glance."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="retired dataset")
        dataset.soft_delete()

    body = staff_client.get(object_url(user, dataset)).content.decode()

    assert "<del>deleted</del>" in body.split("</h1>")[0]
    assert "deleted <time" in body  # ...and when


def test_an_edge_says_when_its_far_end_is_gone(staff_client: Client, user: User) -> None:
    """A deleted source is the case the edge exists for: the derivation still happened and the
    version it consumed is still readable, so the page must not link to it as though the row
    were still there."""
    with acting_as(user):
        source = create_dataset_for(user, name="the source")
        derived = create_dataset_for(user, name="the derived")
        lineage.record_derivation(derived, sources=[source])
        source.soft_delete()

    body = staff_client.get(object_url(user, derived)).content.decode()
    cell = body.split("the source")[1][:700]

    assert ">deleted</del>" in cell  # the <del> carries a title, so match its content
    # The version it consumed is still reachable — that is the whole point of soft delete.
    assert version_url(user, source, 1) in body


def test_three_different_questions_about_deletion(staff_client: Client, user: User) -> None:
    """Was it deleted *at this version*, is this version *current*, and is the row gone *now* —
    a version written before the delete must answer them differently."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="doomed")
        dataset.soft_delete()

    first = staff_client.get(version_url(user, dataset, 1)).content.decode()
    second = staff_client.get(version_url(user, dataset, 2)).content.decode()

    assert "live at this version" in first
    assert "the row is deleted now" in first
    assert "deleted at this version" in second


def test_the_listing_can_show_live_or_deleted_rows(staff_client: Client, user: User) -> None:
    """Three ways to ask, and the default is the one the app itself uses."""
    with acting_as(user):
        kept = create_dataset_for(user, name="still here")
        gone = create_dataset_for(user, name="retired")
        gone.soft_delete()

    default = staff_client.get(model_url(user, Dataset)).content.decode()
    both = staff_client.get(f"{model_url(user, Dataset)}?state=all").content.decode()
    deleted = staff_client.get(f"{model_url(user, Dataset)}?state=deleted").content.decode()

    assert _row_ids(default) == [str(kept.pk)]  # live by default, like `Model.objects`
    assert str(kept.pk) in _row_ids(both) and str(gone.pk) in _row_ids(both)
    assert _row_ids(deleted) == [str(gone.pk)]


def test_the_state_filter_is_not_offered_where_it_makes_no_sense(
    staff_client: Client, user: User
) -> None:
    """`Lineage` has no `deleted_at`: it is append-only, so "live or deleted" is not a question
    it can answer, and a filter that silently did nothing would be worse than none."""
    with acting_as(user):
        source = create_dataset_for(user, name="source")
        derived = create_dataset_for(user, name="derived")
        lineage.record_derivation(derived, sources=[source])

    edges = staff_client.get(reverse("explorer:model", args=[user.pk, "core", "lineage"]))
    body = edges.content.decode()

    assert "Rows to show" not in body
    # ...and asking for it anyway changes nothing rather than raising.
    assert staff_client.get(f"{edges.request['PATH_INFO']}?state=live").status_code == 200


def test_the_model_list_counts_the_retired_rows_separately(
    staff_client: Client, user: User
) -> None:
    """The row count includes soft-deleted rows, so the page has to say how many that is."""
    with acting_as(user):
        create_dataset_for(user, name="kept")
        gone = create_dataset_for(user, name="retired")
        gone.soft_delete()

    body = staff_client.get(index_url(user)).content.decode()
    row = next(chunk for chunk in body.split("<tr>") if ">Dataset</a>" in chunk)

    assert "<td>2</td>" in row  # both rows counted
    assert "<del>1</del>" in row  # one of them retired


# --- the operation and its description ------------------------------------------------------------


def test_the_listing_names_the_operation_that_wrote_each_row(
    staff_client: Client, user: User
) -> None:
    """ "Which step produced this?" is the question a listing was otherwise silent about."""
    with acting_as(user):
        derived = create_dataset_for(user, name="derived", operation="summarise notes")
        typed = create_dataset_for(user, name="typed by a person")

    body = staff_client.get(model_url(user, Dataset)).content.decode()
    table = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0]

    derived_row = next(chunk for chunk in table.split("<tr>") if str(derived.pk) in chunk)
    typed_row = next(chunk for chunk in table.split("<tr>") if str(typed.pk) in chunk)

    assert "summarise notes" in derived_row
    # A row a person wrote has no operation of its own, and says so rather than inventing one.
    assert '<span class="empty">—</span>' in typed_row


def test_the_history_shows_what_the_step_did_in_this_run(staff_client: Client, user: User) -> None:
    """`operation_description` is the longer form for the reviewer; it belongs beside the name
    wherever the name appears, and the history block was dropping it."""
    with acting_as(user):
        source = create_dataset_for(user, name="the source")
        derived = create_dataset_for(
            user,
            name="the derived",
            operation="summarise notes",
            sources=[source],
            operation_description="14 chunks, opus, prompt v3",
        )

    body = staff_client.get(object_url(user, derived)).content.decode()
    history = body.split(">History<", 1)[1].split(">Lineage<", 1)[0]

    assert "summarise notes" in history
    assert "14 chunks, opus, prompt v3" in history  # the description, in the history block
    assert "14 chunks, opus, prompt v3" in body.split(">Lineage<", 1)[1]  # ...and on the edge


def test_the_version_page_names_the_operation_and_its_description(
    staff_client: Client, user: User
) -> None:
    with acting_as(user):
        source = create_dataset_for(user, name="source")
        derived = create_dataset_for(
            user,
            name="derived",
            operation="convert to EUR",
            sources=[source],
            operation_description="rates of 2026-09-01",
        )

    body = staff_client.get(version_url(user, derived, 1)).content.decode()

    assert "operation</dt>" in body
    assert "convert to EUR" in body
    assert "rates of 2026-09-01" in body
