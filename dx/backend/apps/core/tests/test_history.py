"""The versioning, soft-delete and lineage invariants, enforced instead of reviewed.

Everything here is a rule that a new model, a new field or a careless `class Meta:` could break
silently. The behavioural half (a bulk write really does produce version rows, a lineage edge
really does keep pointing at the old value) matters just as much as the structural half: if
capture ever stops, the lineage graph does not go empty, it goes *wrong*.

Tenant isolation of the event tables lives in `test_tenancy.py`, with the rest of the RLS suite.
"""

import json
import uuid
from collections.abc import Callable
from typing import Protocol, cast
from unittest import mock

import pghistory
import pgtrigger
import pytest
from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.db.models import Model, UniqueConstraint
from django.test import Client
from django_scopes import scopes_disabled

from apps.accounts.models import ApiToken, User
from apps.core import history, lineage, revisions
from apps.core.models import VersionedModel
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for
from apps.datasets.models import Dataset, DatasetOptions
from apps.documents.api import store_documents
from apps.documents.models import Blob, Document

pytestmark = pytest.mark.django_db


class DatasetEventRow(history.EventRow, Protocol):
    """A `DatasetEvent` row: the common event columns plus the fields Dataset mirrors."""

    name: str
    description: str
    row_count: int


class DocumentEventRow(history.EventRow, Protocol):
    """A `DocumentEvent` row (see `DatasetEventRow`)."""

    title: str
    size: int


def dataset_events(pk: uuid.UUID) -> list[DatasetEventRow]:
    """Every version of one dataset, newest first, readable as a typed row."""
    return [cast(DatasetEventRow, row) for row in history.event_rows(Dataset, pk)]


def event_type(model: type[Model]) -> ContentType:
    """Content type of `model`'s event table — what a lineage edge's `*_type` points at."""
    event_model = history.event_model_for(model)
    assert event_model is not None, f"{model.__name__} is not tracked"
    return ContentType.objects.get_for_model(event_model)


def concrete_base_models() -> list[type[VersionedModel]]:
    return [
        model
        for model in apps.get_models()
        if issubclass(model, VersionedModel) and not model._meta.abstract
    ]


# --- Coverage: nothing versioned by accident, nothing forgotten -------------------------------


def test_every_base_model_is_tracked_or_exempt() -> None:
    missing = [
        model._meta.label
        for model in concrete_base_models()
        if model._meta.label not in history.HISTORY_EXEMPT
        and history.event_model_for(model) is None
    ]
    assert missing == [], (
        f"untracked models: {missing}. Decorate them with @tracked (apps/core/history.py) or "
        "add them to HISTORY_EXEMPT with a reason."
    )


def test_history_exempt_entries_still_exist() -> None:
    """A stale exemption is worse than none: it silently excuses a model nobody meant to skip."""
    labels = {model._meta.label for model in apps.get_models()}
    assert history.HISTORY_EXEMPT <= labels, history.HISTORY_EXEMPT - labels


def test_tracked_models_are_never_reported_through_inheritance() -> None:
    """`hasattr(model, "pgh_event_model")` is inherited, so coverage must check what the event
    model actually tracks — the trap that makes a wrongly decorated abstract base look fine."""
    for model, event_model in history.tracked_models():
        obj_field = next(f for f in event_model._meta.fields if f.name == "pgh_obj")
        assert obj_field.related_model is model


def test_no_implicit_m2m_on_versioned_models() -> None:
    """An auto-created through table is not a model, so it can be neither tracked nor owned:
    a tag change would leave no version row behind it, which is the one thing lineage cannot
    tolerate. Declare an explicit `through=` model inheriting `OwnedModel`.

    Scoped to `VersionedModel` subclasses — the tables that hold our data. `accounts.User` inherits
    `groups`/`user_permissions` from Django's `AbstractUser`; those are permission bookkeeping
    on a shared table, they carry no tenant data, and they are not ours to redeclare.
    """
    implicit = [
        f"{model._meta.label}.{field.name}"
        for model in concrete_base_models()
        for field in model._meta.many_to_many
        if isinstance(through := field.remote_field.through, type) and through._meta.auto_created
    ]
    assert implicit == [], f"declare an explicit through= model for: {implicit}"


def test_triggers_survived_meta_inheritance() -> None:
    """A concrete model that writes `class Meta:` instead of `class Meta(OwnedModel.Meta)`
    silently loses both triggers — no error, no migration, just no protection."""
    registered: dict[str, set[str]] = {}
    for owner, trigger in pgtrigger.registry.registered():
        registered.setdefault(owner._meta.label, set()).add(str(trigger.name))

    for model in concrete_base_models():
        names = registered.get(model._meta.label, set())
        assert {"no_hard_delete", "bump_version"} <= names, (
            f"{model._meta.label} lost its base triggers ({sorted(names)}) — "
            "does its Meta inherit VersionedModel.Meta?"
        )


def test_every_base_model_hides_soft_deleted_rows_by_default() -> None:
    """`objects` is what application code reaches for; it must not return deleted rows."""
    for model in concrete_base_models():
        with scopes_disabled():  # owned models refuse to build a query without a tenant
            queryset = str(model._default_manager.all().query)
        assert "deleted_at" in queryset, (
            f"{model._meta.label}._default_manager does not filter deleted_at — declare "
            "`objects = ActiveManager()` (see apps/core/models.py::VersionedModel)."
        )
        assert hasattr(model, "all_objects"), f"{model._meta.label} has no all_objects manager"


def test_no_unconditional_unique_constraints() -> None:
    """On a soft-deleted model a plain unique index reserves the value forever: the row is gone
    from the application's point of view but still holds its name."""
    offenders = []
    for model in concrete_base_models():
        offenders += [
            f"{model._meta.label}.{field.name}"
            for field in model._meta.fields
            if field.unique and not field.primary_key
        ]
        offenders += [
            f"{model._meta.label}:{constraint.name}"
            for constraint in model._meta.constraints
            if isinstance(constraint, UniqueConstraint) and constraint.condition is None
        ]
        offenders += [f"{model._meta.label}:{tuple(u)}" for u in model._meta.unique_together]
    assert offenders == [], (
        f"condition these on deleted_at__isnull=True: {offenders} "
        "(see apps/accounts/models.py::ApiToken)"
    )


def test_history_schema_snapshot_is_current() -> None:
    """Adding or dropping a tracked field changes what every older version row means.

    Two different failures, with two different fixes: a *changed* field set under the current
    tag needs a bump (rows already written claim the old set), while a newly tracked model only
    needs the file regenerating — nothing that exists is invalidated by it.
    """
    on_disk = json.loads(history.SCHEMA_FILE.read_text())
    recorded = on_disk.get("tags", {}).get(history.SCHEMA_TAG, {})
    moved = sorted(
        label
        for label, fields in history.tracked_fields().items()
        if label in recorded and recorded[label] != fields
    )
    assert not moved, (
        f"the tracked fields of {moved} changed under schema tag {history.SCHEMA_TAG}, but "
        "every version row written so far claims the old set. Bump SCHEMA_TAG in "
        "apps/core/history.py, then run `manage.py history_schema --write`."
    )
    assert on_disk == history.tracked_schema(), (
        "history_schema.json is out of date; run `manage.py history_schema --write`."
    )


# --- Behaviour: capture cannot be bypassed ----------------------------------------------------


def test_orm_save_versions_and_bumps(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="first")
        assert dataset.version == 1

        dataset.name = "second"
        dataset.save(operation=None, sources=[])
        dataset.refresh_from_db()

        assert dataset.version == 2
        events = dataset_events(dataset.pk)
    assert [(e.version, e.name, e.pgh_label) for e in events] == [
        (2, "second", "update"),
        (1, "first", "insert"),
    ]


def test_queryset_update_is_versioned(user: User) -> None:
    """The reason this is trigger-based at all: `.update()` never calls `save()`."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="before")
        Dataset.objects.filter(pk=dataset.pk).update(name="after")
        dataset.refresh_from_db()

        assert dataset.version == 2
        latest = dataset_events(dataset.pk)[0]
    assert (latest.version, latest.name, latest.pgh_label) == (2, "after", "update")


def test_raw_sql_update_is_versioned(user: User) -> None:
    """A data migration writing SQL by hand must not be able to skip history either."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="before")
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE datasets_dataset SET name = 'raw' WHERE id = %s", [str(dataset.pk)]
            )
        dataset.refresh_from_db()

        assert dataset.version == 2
        assert history.event_rows(Dataset, dataset.pk).count() == 2


def test_modified_never_predates_created(user: User) -> None:
    """`created` and `modified` must come from the same clock (see VersionedModel.Meta)."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        Dataset.objects.filter(pk=dataset.pk).update(name="y")
        dataset.refresh_from_db()
    assert dataset.created <= dataset.modified


def test_python_cannot_forge_version_or_modified(user: User) -> None:
    """Both columns belong to the database; assigning them in Python is simply ignored."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        dataset.version = 99
        dataset.modified = dataset.created
        dataset.save(operation=None, sources=[])
        dataset.refresh_from_db()
    assert dataset.version == 2


def test_event_tables_are_append_only(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        rows = history.event_rows(Dataset, dataset.pk)

        with pytest.raises(Exception, match="append_only"), transaction.atomic():
            rows.update(pgh_label="forged")
        with pytest.raises(Exception, match="append_only"), transaction.atomic():
            rows.delete()


def test_delete_is_soft_and_hard_delete_is_not(user: User) -> None:
    """`delete()` is the soft one — on the instance and on a queryset — and `hard_delete()` is
    the deliberate exception, used by erasure, credential purging and test teardown."""
    with acting_as(user):
        one = create_dataset_for(user, name="one")
        many = create_dataset_for(user, name="many")

        one.delete()
        Dataset.objects.filter(pk=many.pk).delete()

        # Gone from the application, still in the table, and each with a version row for it.
        assert Dataset.objects.count() == 0
        assert Dataset.all_objects.count() == 2
        assert [version.deleted for version in one.history()] == [False, True]

        Dataset.all_objects.filter(pk=one.pk).hard_delete()
        assert not Dataset.all_objects.filter(pk=one.pk).exists()
        assert not history.event_rows(Dataset, one.pk).exists()  # its history goes too


def test_raw_deletes_are_still_refused_by_the_database(user: User) -> None:
    """The override makes `delete()` soft; the trigger is what makes it impossible to lose a
    row *anyway* — through raw SQL, a data migration, or `_base_manager`, none of which the
    Python override can reach."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")

        with pytest.raises(Exception, match="Cannot delete rows"), transaction.atomic():
            Dataset._base_manager.filter(pk=dataset.pk).delete()

        with pytest.raises(Exception, match="Cannot delete rows"), transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute("DELETE FROM datasets_dataset WHERE id = %s", [dataset.pk])


def test_soft_delete_is_a_version(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        dataset.soft_delete()

        assert Dataset.objects.filter(pk=dataset.pk).count() == 0  # gone from the application
        stored = Dataset.all_objects.get(pk=dataset.pk)  # still there, and still versioned
        assert stored.deleted_at is not None
        assert stored.version == 2
        latest = dataset_events(dataset.pk)[0]
    assert latest.deleted_at is not None
    assert latest.version == 2


def test_forward_fk_still_resolves_to_a_soft_deleted_row(user: User) -> None:
    """`_base_manager` must stay unfiltered: `document.owner` is dereferenced in code paths
    nobody wrote, and a soft-deleted row on the far side must not raise there."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        dataset.soft_delete()
        fetched = Dataset.all_objects.get(pk=dataset.pk)
        assert fetched.owner == user


def test_soft_deleted_rows_do_not_reserve_unique_values(user: User) -> None:
    token = ApiToken.create(
        operation=None, sources=[], user=user, name="ci", token="tk_" + "a" * 20
    )
    token.soft_delete()
    again = ApiToken.create(
        operation=None, sources=[], user=user, name="ci again", token="tk_" + "a" * 20
    )
    assert again.pk != token.pk


# --- Reading a version back as the model it is a version of ------------------------------------


def test_history_lists_every_version_oldest_first(user: User) -> None:
    """`obj.history()` reads like a list of the object's past selves, current state last."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="first")
        dataset.name = "second"
        dataset.save(operation=None, sources=[])
        Dataset.objects.filter(pk=dataset.pk).update(name="third")

        past = dataset.history()

    assert [(version.version, version.to_object().name) for version in past] == [
        (1, "first"),
        (2, "second"),
        (3, "third"),
    ]


def test_to_object_returns_the_tracked_model_and_leaves_the_live_row_alone(user: User) -> None:
    """The way back from a generated event row to the model it mirrors — the whole point of
    `Version`: nothing outside `apps/core/history.py` has to know `DatasetEvent` exists."""
    with acting_as(user):
        dataset = create_dataset_for(
            user, name="first", options=DatasetOptions(delimiter=";", has_header=False)
        )
        dataset.name = "second"
        dataset.options = DatasetOptions()
        dataset.save(operation=None, sources=[])

        original = dataset.history()[0].to_object()

        assert isinstance(original, Dataset)
        assert (original.pk, original.name, original.version) == (dataset.pk, "first", 1)
        # Typed JSON columns come back as their pydantic model, not as a dict.
        assert (original.options.delimiter, original.options.has_header) == (";", False)
        # And reading a past state changed nothing about the row itself.
        assert Dataset.objects.get(pk=dataset.pk).name == "second"


def test_to_object_rebuilds_a_file_field_against_the_tracked_model(user: User) -> None:
    """A `FileField` on an event row is bound to the *event* model's field; the rebuilt object
    must carry the tracked model's own descriptor, or it reads through the wrong storage."""
    with acting_as(user):
        (document,) = store_documents(
            user, [SimpleUploadedFile("source.pdf", b"%PDF", content_type="application/pdf")]
        )
        blob = document.source_blob
        stored_key = blob.file.name
        blob.mime_type = "application/x-renamed"
        blob.save(operation=None, sources=[])

        original = blob.history()[0].to_object()

    assert (original.mime_type, original.file.name) == ("application/pdf", stored_key)
    assert original.file.field is Blob._meta.get_field("file")


def test_saving_a_past_version_restores_it_as_a_new_version(user: User) -> None:
    """Restoring is a normal write, never a rewrite: the `bump_version` trigger gives it the
    next number and the event table records that the restore happened."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="first")
        dataset.name = "second"
        dataset.save(operation=None, sources=[])

        dataset.history()[0].to_object().save(operation=None, sources=[])

        dataset.refresh_from_db()
        assert (dataset.name, dataset.version) == ("first", 3)
        assert [version.version for version in dataset.history()] == [1, 2, 3]


def test_a_soft_delete_is_visible_on_the_version_it_happened_in(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        dataset.soft_delete()

        past = dataset.history()
        assert [version.deleted for version in past] == [False, True]
        assert past[-1].to_object().deleted_at is not None


def test_history_of_an_unversioned_model_says_which_rule_applies(user: User) -> None:
    token = ApiToken.create(
        operation=None, sources=[], user=user, name="ci", token="tk_" + "a" * 20
    )
    with pytest.raises(history.NotTracked, match="not versioned"):
        token.history()


def test_a_version_names_the_fields_it_cannot_speak_for(user: User) -> None:
    """A field added later exists on every older event row too, holding whatever the column was
    backfilled with. `to_object()` hands that value over; `untracked_fields()` is the warning
    label on it (see "Schema evolution")."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        current = history.tracked_fields()
        older = [f for f in current[Dataset._meta.label] if f != "description"]
        log = {
            "current": history.SCHEMA_TAG,
            "tags": {"2000-01": {Dataset._meta.label: older}, history.SCHEMA_TAG: current},
        }
        with mock.patch.object(history, "load_schema_log", return_value=log):
            with history.unversioned(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE datasets_datasetevent SET pgh_schema = '2000-01' WHERE pgh_obj_id = %s",
                    [str(dataset.pk)],
                )
            (first,) = dataset.history()

            assert first.untracked_fields() == frozenset({"description"})
            assert first.to_object().description == ""  # the backfilled default, not a value


def test_history_of_another_tenants_row_is_empty(user: User, other_user: User) -> None:
    """`history()` reads the event table directly, so it leans on the same row-level security
    as everything else (see .claude/rules/multitenancy.md)."""
    with acting_as(other_user):
        theirs = create_dataset_for(other_user, name="theirs")
    with acting_as(user):
        assert theirs.history() == []


# --- Context ----------------------------------------------------------------------------------


def test_task_context_names_the_task_without_identifying_the_tenant(user: User) -> None:
    with acting_as(user), history.history_context("task", task="dataset_summary"):
        dataset = create_dataset_for(user, name="x")
        event = history.current_event(dataset)
        assert event.pgh_context_id is not None
        context = pghistory.models.Context.objects.get(pk=event.pgh_context_id)
    assert context.metadata == {"source": "task", "task": "dataset_summary"}


def test_history_context_refuses_identifiers(user: User) -> None:
    """`pghistory_context` is one shared table every tenant can read (apps/core/history.py)."""
    with pytest.raises(ValueError, match="looks like an identifier"):
        with history.history_context("task", tenant=str(user.pk)):
            pass  # pragma: no cover


def test_one_request_groups_its_writes_under_one_context(auth_client: Client, user: User) -> None:
    response = auth_client.post(
        "/api/datasets", data={"name": "via api"}, content_type="application/json"
    )
    assert response.status_code == 201
    dataset_id = uuid.UUID(response.json()["id"])

    with acting_as(user):
        event = history.current_event(Dataset.objects.get(pk=dataset_id))
        assert event.pgh_context_id is not None
        context = pghistory.models.Context.objects.get(pk=event.pgh_context_id)
    assert context.metadata == {"source": "api", "method": "POST"}


# --- Lineage ----------------------------------------------------------------------------------


def test_lineage_pins_the_source_version_and_ignores_later_edits(user: User) -> None:
    """The whole point: the FK follows the live row, the edge does not."""
    with acting_as(user):
        source = create_dataset_for(user, name="original name")
        target = create_dataset_for(user, name="derived")
        (edge,) = lineage.record_derivation(target, sources=[source])

        source.name = "renamed since"
        source.save(operation=None, sources=[])
        source.refresh_from_db()

        assert source.version == 2
        assert edge.source_version == 1
        was = cast(DatasetEventRow, edge.resolve_source())
        assert was.name == "original name"  # not "renamed since"
        assert lineage.stale_derivations(source).count() == 1
        assert lineage.derived_from(source).count() == 1
        assert [e.pk for e in lineage.sources_of(target)] == [edge.pk]


def test_lineage_edges_are_immutable(user: User) -> None:
    with acting_as(user):
        source = create_dataset_for(user, name="s")
        target = create_dataset_for(user, name="t")
        (edge,) = lineage.record_derivation(target, sources=[source])

        with pytest.raises(Exception, match="append_only"), transaction.atomic():
            lineage.Lineage.objects.filter(pk=edge.pk).update(source_version=99)
        with pytest.raises(Exception, match="Cannot delete rows"), transaction.atomic():
            lineage.Lineage.objects.filter(pk=edge.pk).delete()


def test_lineage_records_one_edge_per_source(user: User) -> None:
    with acting_as(user):
        a = create_dataset_for(user, name="a")
        b = create_dataset_for(user, name="b")
        target = create_dataset_for(user, name="t")
        edges = lineage.record_derivation(target, sources=[a, b])

        assert {e.source_obj_id for e in edges} == {a.pk, b.pk}
        assert lineage.sources_of(target).count() == 2
        # A second identical edge is a no-op the database refuses, not a duplicate row.
        with pytest.raises(IntegrityError), transaction.atomic():
            lineage.record_derivation(target, sources=[a])


def test_lineage_needs_a_versioned_source(user: User) -> None:
    with acting_as(user):
        target = create_dataset_for(user, name="t")
        token = ApiToken.create(operation=None, sources=[], user=user, name="unversioned")
        with pytest.raises(history.NotTracked, match="not versioned"):
            lineage.record_derivation(target, sources=[token])


def test_lineage_points_at_event_models_not_live_models(user: User) -> None:
    """`target_type`/`source_type` name the *event* table: an edge addresses a version."""
    with acting_as(user):
        source = create_dataset_for(user, name="s")
        target = create_dataset_for(user, name="t")
        (edge,) = lineage.record_derivation(target, sources=[source])
    assert edge.source_type == event_type(Dataset)
    assert edge.target_type == event_type(Dataset)


def test_lineage_survives_a_soft_deleted_source(user: User) -> None:
    """A source that is deleted afterwards must still resolve — that is why deletes are soft."""
    with acting_as(user):
        source = create_dataset_for(user, name="the source")
        target = create_dataset_for(user, name="t")
        (edge,) = lineage.record_derivation(target, sources=[source])
        source.soft_delete()

        assert Dataset.objects.filter(pk=source.pk).count() == 0
        assert cast(DatasetEventRow, edge.resolve_source()).name == "the source"


def test_lineage_spans_models(user: User) -> None:
    """Edges are generic: a dataset built from an uploaded document links the two event tables,
    and the document's name at that moment is what the edge resolves to."""
    with acting_as(user):
        (document,) = store_documents(
            user, [SimpleUploadedFile("source.pdf", b"%PDF", content_type="application/pdf")]
        )
        dataset = create_dataset_for(user, name="imported")
        (edge,) = lineage.record_derivation(dataset, sources=[document])

        document.title = "renamed.pdf"
        document.save(operation=None, sources=[])

        assert edge.source_type == event_type(Document)
        assert edge.target_type == event_type(Dataset)
        assert cast(DocumentEventRow, edge.resolve_source()).title == "source.pdf"
        assert edge.resolve_target().id == dataset.pk


def test_sources_hands_back_the_source_as_its_own_model(user: User) -> None:
    """The lineage counterpart of `to_object()`: an edge resolves to a `Document`, not to a
    `DocumentEvent` nobody imports."""
    with acting_as(user):
        (document,) = store_documents(
            user, [SimpleUploadedFile("source.pdf", b"%PDF", content_type="application/pdf")]
        )
        dataset = create_dataset_for(user, name="imported")
        lineage.record_derivation(dataset, sources=[document])

        # Unfiltered, an edge can point at any model, so all it promises is a
        # `VersionedModel` — `str()` still names the row. Name the model to get it typed.
        (any_source,) = dataset.sources()
        assert str(any_source.to_object()) == "source.pdf"

        (source,) = dataset.sources(Document)

        assert (source.to_object().title, source.version) == ("source.pdf", 1)
        assert source.object_id == document.pk
        # And the other direction: what came out of the document.
        assert [v.to_object().name for v in document.derived(Dataset)] == ["imported"]


def test_sources_survive_a_later_edit_of_the_derived_row(user: User) -> None:
    """An edge is recorded against the version that consumed the source, so `sources_of` (the
    current version's edges) empties on the next edit. "What was this built from" must not."""
    with acting_as(user):
        source = create_dataset_for(user, name="the source")
        target = create_dataset_for(user, name="derived")
        lineage.record_derivation(target, sources=[source])

        target.name = "derived, renamed"
        target.save(operation=None, sources=[])

        assert lineage.sources_of(target).count() == 0  # v2 consumed nothing
        assert [v.to_object().name for v in target.sources(Dataset)] == ["the source"]


def test_a_version_names_what_that_version_consumed(user: User) -> None:
    with acting_as(user):
        source = create_dataset_for(user, name="the source")
        target = create_dataset_for(user, name="derived")
        lineage.record_derivation(target, sources=[source])
        target.name = "renamed"
        target.save(operation=None, sources=[])

        first, second = target.history()

        assert [v.to_object().name for v in first.sources(Dataset)] == ["the source"]
        assert second.sources() == []  # the rename consumed nothing
        assert [v.object_id for v in source.history()[0].derived()] == [target.pk]


def test_a_source_version_says_whether_it_is_still_current(user: User) -> None:
    """Which is what makes a derivation stale: the source has moved on since it was read."""
    with acting_as(user):
        source = create_dataset_for(user, name="original")
        target = create_dataset_for(user, name="derived")
        lineage.record_derivation(target, sources=[source])

        assert target.sources()[0].is_current() is True

        source.name = "renamed since"
        source.save(operation=None, sources=[])

        assert target.sources()[0].is_current() is False
        assert lineage.stale_derivations(source).count() == 1


def test_one_source_version_feeding_two_versions_is_listed_once(user: User) -> None:
    """A merge that reads the same source twice must not report it twice."""
    with acting_as(user):
        source = create_dataset_for(user, name="the source")
        target = create_dataset_for(user, name="derived")
        lineage.record_derivation(target, sources=[source])
        target.name = "renamed"
        target.save(operation=None, sources=[])
        lineage.record_derivation(target, sources=[source])  # the same source version again

        assert [v.version for v in target.sources()] == [1]


def test_lineage_of_an_unversioned_model_says_so(user: User) -> None:
    token = ApiToken.create(
        operation=None, sources=[], user=user, name="ci", token="tk_" + "b" * 20
    )
    with pytest.raises(history.NotTracked, match="no lineage"):
        token.sources()


def test_recording_a_derivation_needs_a_tenant_context(user: User) -> None:
    with acting_as(user):
        source = create_dataset_for(user, name="s")
        target = create_dataset_for(user, name="t")
    with pytest.raises(Exception, match="tenant"):
        lineage.record_derivation(target, sources=[source])


def test_lineage_is_covered_by_the_tenant_policy() -> None:
    from apps.core import rls  # noqa: PLC0415

    assert lineage.Lineage._meta.db_table in rls.isolated_tables()


def test_event_tables_are_covered_by_the_tenant_policy() -> None:
    from apps.core import rls  # noqa: PLC0415

    protected = set(rls.isolated_tables())
    for _model, event_model in history.tracked_models():
        assert event_model._meta.db_table in protected


def test_unversioned_replays_rows_without_touching_the_chain(user: User) -> None:
    """What `loaddata` relies on: restoring a row must not bump it or write history again."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        before = history.event_rows(Dataset, dataset.pk).count()

        with history.unversioned():
            Dataset.objects.filter(pk=dataset.pk).update(name="restored")
        dataset.refresh_from_db()

        assert dataset.version == 1  # untouched
        assert history.event_rows(Dataset, dataset.pk).count() == before


def test_model_registry_has_no_stray_event_tables() -> None:
    """Every event table belongs to a model we track on purpose."""
    tracked = {event_model for _model, event_model in history.tracked_models()}
    generated = set(history.event_models())
    assert generated == tracked, generated ^ tracked


def test_lineage_is_not_a_base_model() -> None:
    """It has no version chain of its own and is never soft-deleted (see apps/core/lineage.py)."""
    assert not issubclass(lineage.Lineage, VersionedModel)
    assert lineage.Lineage._meta.get_field("owner") is not None  # but it is still tenant data


# --- The revision endpoint (GET /api/history/{resource}/{id}) ----------------------------------


def test_history_endpoint_groups_saves_and_diffs_them(auth_client: Client, user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="first", description="")
    response = auth_client.patch(
        f"/api/datasets/{dataset.pk}",
        data={"name": "second", "description": "now with a description"},
        content_type="application/json",
    )
    assert response.status_code == 200

    body = auth_client.get(f"/api/history/dataset/{dataset.pk}").json()

    assert (body["model"], body["current_version"]) == ("Dataset", 2)
    assert [group["source"] for group in body["groups"]] == ["api", "unknown"]

    newest = body["groups"][0]["revisions"][0]
    assert newest["version"] == 2
    assert newest["label"] == "update"
    assert newest["schema_known"] is True
    assert newest["unknown_fields"] == []
    assert newest["changes"] == [
        {"field": "description", "old": "", "new": "now with a description"},
        {"field": "name", "old": "first", "new": "second"},
    ]

    oldest = body["groups"][1]["revisions"][0]
    assert oldest["version"] == 1
    assert oldest["label"] == "insert"
    assert {change["field"] for change in oldest["changes"]} == {"name", "options"}
    (options,) = [c for c in oldest["changes"] if c["field"] == "options"]
    # Typed JSON columns render as JSON, not as a pydantic repr.
    assert json.loads(options["new"])["delimiter"] == ","


def test_history_endpoint_shows_the_soft_delete_as_a_version(
    auth_client: Client, user: User
) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="doomed")
    assert auth_client.delete(f"/api/datasets/{dataset.pk}").status_code == 204

    body = auth_client.get(f"/api/history/dataset/{dataset.pk}").json()

    assert body["current_version"] == 2
    newest = body["groups"][0]["revisions"][0]
    assert newest["deleted"] is True
    assert [change["field"] for change in newest["changes"]] == ["deleted_at"]


def test_history_endpoint_links_to_the_source_version_it_consumed(
    auth_client: Client, user: User
) -> None:
    with acting_as(user):
        source = create_dataset_for(user, name="the source")
        derived = create_dataset_for(user, name="derived")
        lineage.record_derivation(derived, sources=[source])
        source.name = "renamed since"
        source.save(operation=None, sources=[])

    body = auth_client.get(f"/api/history/dataset/{derived.pk}").json()

    (ref,) = body["groups"][0]["revisions"][0]["sources"]
    assert ref["model"] == "Dataset"
    assert ref["label"] == "the source"  # the name as it stood, not "renamed since"
    assert ref["version"] == 1
    assert ref["is_stale"] is True


def test_history_endpoint_is_tenant_isolated(
    auth_client: Client, user: User, other_user: User, client_for: Callable[[User], Client]
) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="mine")

    assert auth_client.get(f"/api/history/dataset/{dataset.pk}").status_code == 200
    theirs = client_for(other_user)
    assert theirs.get(f"/api/history/dataset/{dataset.pk}").status_code == 404


def test_history_endpoint_rejects_an_unknown_resource(auth_client: Client, user: User) -> None:
    response = auth_client.get(f"/api/history/apitoken/{uuid.uuid7()}")
    assert response.status_code == 404
    assert "apitoken" in response.json()["detail"]


def test_history_endpoint_requires_authentication(client: Client, user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="mine")
    assert client.get(f"/api/history/dataset/{dataset.pk}").status_code == 401


def test_history_resource_names_are_unique() -> None:
    """The URL segment is derived from the model name, so two apps must not clash."""
    names = [model._meta.model_name for model, _ in history.tracked_models()]
    assert len(names) == len(set(names)), names


def test_a_row_written_under_an_unknown_schema_tag_is_not_diffed(user: User) -> None:
    """The point of `pgh_schema`: a row we cannot describe is reported as unknown, never as a
    change from a value the object never held."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        history.event_rows(Dataset, dataset.pk)  # written under the current tag
        with history.unversioned(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE datasets_datasetevent SET pgh_schema = '1999-01' WHERE pgh_obj_id = %s",
                [str(dataset.pk)],
            )
        (revision,) = revisions.revisions_of(Dataset.objects.get(pk=dataset.pk))

    assert revision.schema_known is False
    assert revision.changes == []
    assert revision.unknown_fields == ["*"]


# --- One save, several tables -----------------------------------------------------------------


def test_one_save_spanning_two_tables_is_one_revision(auth_client: Client, user: User) -> None:
    """The case the context id exists for: a PATCH that renames a dataset *and* adds a tag
    writes DatasetEvent and DatasetTagEvent, and the page shows one revision, not two."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="before")

    response = auth_client.patch(
        f"/api/datasets/{dataset.pk}",
        data={"name": "after", "tags": ["sales"]},
        content_type="application/json",
    )
    assert response.status_code == 200

    body = auth_client.get(f"/api/history/dataset/{dataset.pk}").json()

    newest = body["groups"][0]
    assert newest["source"] == "api"
    assert [(r["model"], r["is_related"]) for r in newest["revisions"]] == [
        ("Dataset", False),
        ("DatasetTag", True),
    ]
    (own, related) = newest["revisions"]
    assert own["changes"] == [{"field": "name", "old": "before", "new": "after"}]
    # The child row is described, not diffed: its columns are foreign keys.
    assert related["description"] == "sales"
    assert related["changes"] == []
    assert related["label"] == "insert"


def test_removing_a_tag_shows_as_a_deleted_child_row(auth_client: Client, user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="x", tags=["sales"])

    auth_client.patch(
        f"/api/datasets/{dataset.pk}", data={"tags": []}, content_type="application/json"
    )

    body = auth_client.get(f"/api/history/dataset/{dataset.pk}").json()
    newest = body["groups"][0]["revisions"][0]

    assert (newest["model"], newest["is_related"], newest["deleted"]) == ("DatasetTag", True, True)
    assert newest["description"] == "sales"
    # The dataset row itself was untouched, so it has no revision in this group.
    assert [r["model"] for r in body["groups"][0]["revisions"]] == ["DatasetTag"]


def test_child_rows_of_another_tenant_never_appear(
    auth_client: Client, user: User, other_user: User
) -> None:
    with acting_as(other_user):
        create_dataset_for(other_user, name="theirs", tags=["secret"])
    with acting_as(user):
        mine = create_dataset_for(user, name="mine", tags=["mine"])

    body = auth_client.get(f"/api/history/dataset/{mine.pk}").json()
    descriptions = [r["description"] for group in body["groups"] for r in group["revisions"]]

    assert "secret" not in descriptions


# --- Schema evolution -------------------------------------------------------------------------


def test_a_field_added_after_a_version_is_reported_as_unknown(user: User) -> None:
    """The reason `pgh_schema` is on every row: `description` did not exist under the older tag,
    so its value there is a backfilled default and must not be shown as a change."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        Dataset.objects.filter(pk=dataset.pk).update(description="added later")

        current = history.tracked_fields()
        older = [f for f in current[Dataset._meta.label] if f != "description"]
        log = {
            "current": history.SCHEMA_TAG,
            "tags": {
                "2000-01": {Dataset._meta.label: older},
                history.SCHEMA_TAG: current,
            },
        }
        with mock.patch.object(history, "load_schema_log", return_value=log):
            # Pretend the first version was written before `description` was tracked.
            with history.unversioned(), connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE datasets_datasetevent SET pgh_schema = '2000-01'"
                    " WHERE pgh_obj_id = %s AND version = 1",
                    [str(dataset.pk)],
                )
            newest, oldest = revisions.revisions_of(Dataset.objects.get(pk=dataset.pk))

    assert oldest.schema_tag == "2000-01"
    assert newest.schema_known is True
    # Not "description: '' → 'added later'": the older row never held a description at all.
    assert [c.field for c in newest.changes] == []
    assert newest.unknown_fields == ["description"]


def test_archived_values_of_dropped_fields_are_shown(user: User) -> None:
    """`pgh_archive` is written by a drop-column migration (apps/core/history.py) and frozen at
    that moment; the page renders it beside the diff instead of losing the value."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="x")
        with history.unversioned(), connection.cursor() as cursor:
            cursor.execute(
                "UPDATE datasets_datasetevent"
                " SET pgh_archive = jsonb_build_object('legacy_slug', 'orders-2026')"
                " WHERE pgh_obj_id = %s",
                [str(dataset.pk)],
            )
        (revision,) = revisions.revisions_of(Dataset.objects.get(pk=dataset.pk))

    assert revision.archived == {"legacy_slug": "orders-2026"}


def test_the_schema_log_keeps_older_tags(user: User) -> None:
    """Regenerating after a tag bump must not rewrite what an older tag meant — that record is
    the only thing that can explain a row written under it."""
    log = {"current": "2000-01", "tags": {"2000-01": {"datasets.Dataset": ["name"]}}}
    with mock.patch.object(history, "load_schema_log", return_value=log):
        regenerated = history.tracked_schema()

    assert regenerated["current"] == history.SCHEMA_TAG
    assert regenerated["tags"]["2000-01"] == {"datasets.Dataset": ["name"]}
    assert "name" in regenerated["tags"][history.SCHEMA_TAG]["datasets.Dataset"]


def test_a_saved_instance_reports_the_version_the_database_gave_it(user: User) -> None:
    """`version` and `modified` are written by a trigger, so the instance the API serialises
    back has to read them again — otherwise every write response reports the previous state."""
    with acting_as(user):
        dataset = create_dataset_for(user, name="first")
        before = dataset.modified

        dataset.name = "second"
        dataset.save(operation=None, sources=[])

        assert dataset.version == 2  # not the stale 1 the instance was holding
        assert dataset.modified > before
        stored = Dataset.objects.get(pk=dataset.pk)
    assert (stored.version, stored.modified) == (dataset.version, dataset.modified)


def test_the_write_endpoints_report_the_new_version(auth_client: Client, user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="first")

    patched = auth_client.patch(
        f"/api/datasets/{dataset.pk}", data={"name": "second"}, content_type="application/json"
    )

    assert patched.json()["version"] == 2
