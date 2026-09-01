"""The multitenancy contract (CLAUDE.md "Multitenancy"), checked for every owned model at once
so new apps are covered automatically:

- ORM layer: outside a scope an owned queryset raises; inside, only the scope's rows exist.
- Database layer: row-level security hides and rejects other users' rows even for raw SQL and
  with the ORM scope switched off; without a context every owned table is empty (fails closed).
- Request layer: the middleware sets the context from the verified bearer token only.
- Tasks, cache keys, file paths, the scrub registry and the source-level rules.

`test_ownership.py` covers the HTTP side (empty lists, 404 for foreign objects).
"""

import re
import uuid
from collections.abc import Callable
from pathlib import Path

import pytest
from django.conf import settings
from django.core.checks import Tags, run_checks
from django.core.checks.registry import registry as check_registry
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError, IntegrityError, connection, models, transaction
from django.http import HttpRequest, HttpResponse
from django.test import Client, RequestFactory
from django.test.utils import isolate_apps
from django.utils import timezone
from django_scopes import ScopeError, get_scope, scope, scopes_disabled
from pytest_django import DjangoAssertNumQueries
from pytest_django.fixtures import Settings

from apps.accounts import api as accounts_api
from apps.accounts.api import issue_access_token
from apps.accounts.models import User
from apps.core import cache as tenant_cache
from apps.core import checks, db, health, middleware, rls, scrub, tasks, tenants
from apps.core.db import NoTenantContext
from apps.core.history import hard_delete
from apps.core.models import OwnedModel
from apps.core.testing import acting_as
from apps.datasets.api import create_dataset_for
from apps.datasets.models import Dataset
from apps.documents.api import store_documents
from config.env import BASE_DIR

pytestmark = pytest.mark.django_db

OWNED_MODELS = rls.tenant_models()
TENANT_APP_DIRS = [BASE_DIR / "apps" / label for label in checks.tenant_app_labels()]


@pytest.fixture(autouse=True)
def media_root(settings: Settings, tmp_path: Path) -> Path:
    settings.MEDIA_ROOT = tmp_path
    return tmp_path


# --- generic fixtures ----------------------------------------------------------------------------


def minimal_kwargs(model: type[OwnedModel], owner: User) -> dict[str, object]:
    """Values for every required field of `model`, by field type — extend when a new field
    type shows up, so that the parametrised tests below keep covering every owned model."""
    values: dict[str, object] = {}
    for field in model._meta.concrete_fields:
        if field.primary_key or not field.editable or field.has_default():
            continue
        if field.blank and not isinstance(field, models.FileField):
            continue
        if isinstance(field, models.ForeignKey):
            # A join model (an explicit m2m through) needs its ends; build them for the same
            # owner, or the WITH CHECK clause would reject the row for the wrong reason.
            target = field.related_model
            assert issubclass(target, OwnedModel), f"{model.__name__}.{field.name} -> {target}"
            values[field.name] = create_owned(target, owner)
        elif isinstance(field, models.FileField):
            values[field.name] = ContentFile(b"x", name="x.bin")
        elif field.choices:
            values[field.name] = field.choices[0][0]
        elif isinstance(field, models.CharField | models.TextField):
            # Unique per call: a model with a conditional unique constraint (Tag.name) is built
            # more than once per test, and a fixed "x" would collide on the second one.
            values[field.name] = f"x{uuid.uuid4().hex[:8]}"[: field.max_length or 9]
        elif isinstance(field, models.IntegerField):
            values[field.name] = 1
        elif isinstance(field, models.BooleanField):
            values[field.name] = False
        elif isinstance(field, models.DateTimeField):
            values[field.name] = timezone.now()
        else:  # pragma: no cover - reached only when a new field type is added
            raise NotImplementedError(f"minimal_kwargs: add a value for {model}.{field.name}")
    return values


def create_owned(model: type[OwnedModel], owner: User) -> OwnedModel:
    kwargs = minimal_kwargs(model, owner)  # may create the rows this one points at
    with acting_as(owner):
        return model.objects.create(owner=owner, **kwargs)


def guc() -> str:
    with connection.cursor() as cursor:
        cursor.execute("SELECT coalesce(current_setting(%s, true), '')", [db.guc_name()])
        row = cursor.fetchone()
    return str(row[0]) if row else ""


def count_rows(table: str, where: str = "", params: list[uuid.UUID] | None = None) -> int:
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT count(*) FROM {connection.ops.quote_name(table)} {where}", params)
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def label(model: type[models.Model]) -> str:
    return model._meta.label


# --- both isolation layers, every owned model --------------------------------------------------


def test_every_feature_app_has_owned_models_only() -> None:
    assert OWNED_MODELS, "no owned models found"
    assert checks.tenant_model_errors(rls.tenant_models()) == []
    assert {m._meta.app_label for m in OWNED_MODELS} == checks.tenant_app_labels()


@pytest.mark.parametrize("model", OWNED_MODELS, ids=label)
def test_owned_model_is_isolated_by_orm_scope_and_rls(
    model: type[OwnedModel], user: User, other_user: User
) -> None:
    mine = create_owned(model, user)
    theirs = create_owned(model, other_user)
    table = model._meta.db_table

    with acting_as(user):
        assert list(model.objects.all()) == [mine]
        assert model.objects.filter(pk=theirs.pk).exists() is False
        assert list(model.objects.for_user(other_user)) == []  # an explicit filter cannot widen
        # Raw SQL bypasses the ORM scope; the policy still hides the other row.
        assert count_rows(table) == 1
        assert count_rows(table, "WHERE owner_id = %s", [other_user.pk]) == 0
        with scopes_disabled():  # ORM layer off: the database alone keeps it to one tenant
            assert model.objects.count() == 1
    with acting_as(other_user):
        assert list(model.objects.all()) == [theirs]


@pytest.mark.parametrize("model", OWNED_MODELS, ids=label)
def test_no_context_sees_nothing(model: type[OwnedModel], user: User) -> None:
    """Fails closed: without a tenant context the ORM refuses and the table is empty."""
    create_owned(model, user)
    assert guc() == ""

    with pytest.raises(ScopeError, match="No tenant scope"):
        model.objects.count()
    with scopes_disabled():
        assert model.objects.count() == 0
    assert count_rows(model._meta.db_table) == 0


@pytest.mark.parametrize("model", OWNED_MODELS, ids=label)
def test_with_check_rejects_foreign_owner(
    model: type[OwnedModel], user: User, other_user: User
) -> None:
    """The WITH CHECK half of the policy: writes for another owner fail, whatever the ORM says."""
    mine = create_owned(model, user)
    # Built up front: a join model's ends have to exist before the write that must fail, or the
    # failure could come from the missing row rather than from the policy.
    foreign = minimal_kwargs(model, user)

    with acting_as(user):
        with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
            model.objects.create(owner=other_user, **foreign)
        with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
            with scopes_disabled():
                model.objects.filter(pk=mine.pk).update(owner=other_user)
    with acting_as(other_user):
        assert model.objects.filter(pk=mine.pk).exists() is False  # nothing moved


def test_save_fills_owner_from_the_context(user: User) -> None:
    with acting_as(user):
        dataset = Dataset(name="implicit")
        dataset.save()
        assert dataset.owner == user

    with pytest.raises(NoTenantContext, match="tenant_context"):
        Dataset(name="orphan").save()


def test_scope_needs_the_users_primary_key(user: User) -> None:
    with scope(user=str(user.pk)), pytest.raises(ScopeError, match="primary key"):
        Dataset.objects.count()


def test_contexts_nest_and_restore_the_previous_tenant(user: User, other_user: User) -> None:
    with acting_as(user):
        with acting_as(other_user):
            assert guc() == str(other_user.pk)
            assert db.current_user_id.get() == other_user.pk
            assert get_scope()["user"] == other_user.pk
        assert guc() == str(user.pk)
        assert db.current_user_id.get() == user.pk
    assert guc() == ""
    assert db.current_user_id.get() is None
    assert get_scope() == {}


def test_a_failing_block_leaves_no_context_behind(user: User) -> None:
    with pytest.raises(RuntimeError, match="boom"), acting_as(user):
        raise RuntimeError("boom")
    assert guc() == ""
    assert db.current_user_id.get() is None

    with pytest.raises(DatabaseError, match="row-level security"), acting_as(user):
        Dataset.objects.create(owner=User.objects.create_user("carol"), name="foreign")
    assert guc() == ""  # a database error rolled the block back, including the SET LOCAL


# --- request layer -----------------------------------------------------------------------------


def _capturing_middleware(seen: dict[str, object]) -> middleware.TenantMiddleware:
    def view(request: HttpRequest) -> HttpResponse:
        seen.update(
            guc=guc(),
            context=db.current_user_id.get(),
            scope=get_scope().get("user"),
            user=getattr(request, "user", None),  # RequestFactory: no AuthenticationMiddleware
        )
        return HttpResponse()

    return middleware.TenantMiddleware(view)


def test_middleware_opens_the_context_for_a_valid_bearer_token(user: User) -> None:
    seen: dict[str, object] = {}
    request = RequestFactory().get(
        "/api/anything", headers={"Authorization": f"Bearer {issue_access_token(user)}"}
    )

    _capturing_middleware(seen)(request)

    assert seen == {"guc": str(user.pk), "context": user.pk, "scope": user.pk, "user": user}
    assert guc() == ""  # gone with the request's transaction
    assert db.current_user_id.get() is None


@pytest.mark.parametrize("token", ["nope", ""], ids=["invalid", "empty"])
def test_middleware_gives_no_context_without_a_valid_token(token: str) -> None:
    seen: dict[str, object] = {}
    request = RequestFactory().get("/api/anything", headers={"Authorization": f"Bearer {token}"})

    _capturing_middleware(seen)(request)

    assert (seen["guc"], seen["context"], seen["scope"]) == ("", None, None)


def test_middleware_ignores_tokens_outside_the_api(
    user: User, django_assert_num_queries: DjangoAssertNumQueries
) -> None:
    seen: dict[str, object] = {}

    def view(request: HttpRequest) -> HttpResponse:  # no SQL of its own
        seen["context"] = db.current_user_id.get()
        return HttpResponse()

    request = RequestFactory().get(
        "/admin/", headers={"Authorization": f"Bearer {issue_access_token(user)}"}
    )

    with django_assert_num_queries(0):  # the token is not even looked at
        middleware.TenantMiddleware(view)(request)

    assert seen == {"context": None}


def test_bearer_token_is_verified_exactly_once(
    auth_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []
    original = accounts_api.authenticate_bearer

    def counting(token: str) -> User | None:
        calls.append(token)
        return original(token)

    monkeypatch.setattr(accounts_api, "authenticate_bearer", counting)

    assert auth_client.get("/api/auth/me").status_code == 200
    assert len(calls) == 1  # the middleware's verification is reused by BearerAuth


def test_session_login_never_authenticates_the_api(client: Client, staff_user: User) -> None:
    client.force_login(staff_user)

    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/datasets").status_code == 401


# --- tasks -------------------------------------------------------------------------------------


def test_tenant_task_runs_in_the_owners_context(user: User, other_user: User) -> None:
    with acting_as(user):
        create_dataset_for(user, name="a", row_count=2)
    with acting_as(other_user):
        create_dataset_for(other_user, name="b", row_count=5)

    assert tasks.dataset_summary.delay(user.pk).get() == {"datasets": 1, "rows": 2}
    # Ids arrive as strings from a JSON-serialised task message.
    assert tasks.dataset_summary.delay(str(other_user.pk)).get() == {"datasets": 1, "rows": 5}
    assert guc() == ""  # nothing leaks out of the task
    assert db.current_user_id.get() is None


def test_tenant_apps_define_tasks_with_tenant_task_only() -> None:
    """`@shared_task` in a feature app would run without a tenant context."""
    pattern = re.compile(r"@(shared_task|app\.task)\b")
    offenders = [
        str(path.relative_to(BASE_DIR))
        for app_dir in TENANT_APP_DIRS
        for path in app_dir.rglob("*.py")
        if pattern.search(path.read_text())
    ]
    assert offenders == [], f"use @tenant_task (apps/core/tasks.py) in: {offenders}"


# --- peripheral guardrails ---------------------------------------------------------------------


def test_tenant_apps_do_not_touch_the_raw_cache() -> None:
    """A cache key without the user id serves one user's data to the next."""
    pattern = re.compile(
        r"from django\.core\.cache import|\bcache\.(get|set|add|delete|get_or_set)\("
    )
    offenders = [
        str(path.relative_to(BASE_DIR))
        for app_dir in TENANT_APP_DIRS
        for path in app_dir.rglob("*.py")
        if pattern.search(path.read_text())
    ]
    assert offenders == [], f"use apps.core.cache.tenant_cache_* in: {offenders}"


def test_tenant_cache_keys_carry_the_user(user: User) -> None:
    with pytest.raises(LookupError, match="tenant scope"):
        tenant_cache.tenant_cache_key("report")
    with acting_as(user):
        assert tenant_cache.tenant_cache_key("report") == f"tenant:{user.pk}:report"
        tenant_cache.tenant_cache_set("report", {"rows": 3})
        assert tenant_cache.tenant_cache_get("report") == {"rows": 3}


def test_files_are_stored_under_the_owners_prefix(user: User) -> None:
    with acting_as(user):
        (document,) = store_documents(
            user, [SimpleUploadedFile("r.pdf", b"%PDF", content_type="application/pdf")]
        )
    assert re.fullmatch(rf"documents/{user.pk}/\d{{4}}/\d{{2}}/r\S*\.pdf", str(document.file.name))


def test_scrubbers_cover_every_pii_field(user: User) -> None:
    assert scrub.check_scrubbers() == []

    scrubbed = scrub.scrub(user, 7)
    assert isinstance(scrubbed, User)
    assert scrubbed.email == "user7@example.invalid"
    assert not scrubbed.has_usable_password()
    assert (scrubbed.first_name, scrubbed.last_name, scrubbed.last_login) == ("", "", None)
    assert scrubbed.username == "alice"  # identifies the tenant; not personal data here


@isolate_apps("apps.datasets")
def test_new_pii_fields_refuse_to_export_unscrubbed() -> None:
    class Contact(models.Model):  # plain model: the isolated registry cannot resolve the owner FK
        email = models.CharField(max_length=100)

        class Meta:
            app_label = "datasets"

    assert scrub.missing_scrubbers(Contact) == ["email"]
    with pytest.raises(scrub.UnscrubbedField, match="email"):
        scrub.scrub(Contact(email="x@example.com"))


# --- system checks -----------------------------------------------------------------------------


@isolate_apps("apps.datasets")
def test_check_flags_unowned_models_and_auto_m2m_in_tenant_apps() -> None:
    class Rogue(models.Model):  # a feature model that forgot OwnedModel
        class Meta:
            app_label = "datasets"

    class Tag(OwnedModel):
        class Meta(OwnedModel.Meta):
            app_label = "datasets"

    class Tagged(OwnedModel):
        tags = models.ManyToManyField(Tag)  # auto-created through table: no owner column

        class Meta(OwnedModel.Meta):
            app_label = "datasets"

    class Shared(models.Model):
        class Meta:
            app_label = "core"  # not a tenant app: not checked

    errors = checks.tenant_model_errors([Rogue, Tag, Tagged, Shared])

    assert [(e.id, e.obj) for e in errors] == [("tenant.E001", Rogue), ("tenant.E002", Tagged)]


def test_check_rls_applied_is_quiet_on_a_synced_database() -> None:
    assert rls.verify() == []
    assert checks.check_rls_applied(None, ["default"]) == []
    assert checks.check_rls_applied(None, None) == []  # no database requested


@pytest.mark.cross_tenant  # DROP POLICY needs the table owner
def test_check_rls_applied_reports_drift_and_sync_repairs_it() -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"DROP POLICY {rls.POLICY} ON datasets_dataset")

    problems = rls.verify()
    assert problems == [f"datasets_dataset: policy '{rls.POLICY}' missing"]
    errors = checks.check_rls_applied(None, ["default"])
    assert [e.id for e in errors] == ["tenant.E003"]
    assert "datasets_dataset" in errors[0].msg

    assert "datasets_dataset" in rls.sync()
    assert rls.verify() == []


def test_readiness_reports_rls_for_the_runtime_role() -> None:
    assert rls.connection_bypasses_rls() is None
    check = health.check_rls()
    assert check.ok, check.detail
    assert check.detail == f"{len(rls.isolated_tables())} tables, role {rls.APP_ROLE}"


@pytest.mark.cross_tenant
def test_readiness_fails_when_the_connection_bypasses_rls() -> None:
    """A web process connected as the owner/superuser would see every tenant: not ready."""
    reason = rls.connection_bypasses_rls()
    assert reason is not None and rls.current_role() in reason

    check = health.check_rls()
    assert not check.ok
    assert reason in check.detail and "DB_ROLE" in check.detail


def test_settings_derive_tenant_apps_from_installed_apps() -> None:
    assert settings.SHARED_APPS == ["apps.core", "apps.accounts"]
    assert settings.TENANT_APPS == [
        "apps.datasets",
        "apps.documents",
        "apps.gallery",
        "apps.notes",
    ]
    assert settings.SHARED_MODELS == []
    assert isinstance(uuid.UUID(str(uuid.uuid7())), uuid.UUID)  # ids are UUIDs, as the policy casts


def test_middleware_type_hint_for_get_response_is_callable() -> None:
    handler: Callable[[HttpRequest], HttpResponse] = lambda request: HttpResponse()  # noqa: E731
    assert isinstance(middleware.TenantMiddleware(handler), middleware.TenantMiddleware)


# --- second review round: worker path, reconnects, every drift kind, edge cases ----------------


@pytest.mark.django_db(transaction=True)
def test_worker_path_pins_the_session_and_clears_it_afterwards(
    user: User, other_user: User
) -> None:
    """`tenant_task` in a real worker: a pinned session-level context while the function runs,
    cleared when it returns so the next task on this worker cannot inherit the tenant."""
    with acting_as(user):
        create_dataset_for(user, name="mine")
    with acting_as(other_user):
        create_dataset_for(other_user, name="theirs")
    seen: dict[str, object] = {}

    def work(owner_id: uuid.UUID) -> int:
        seen.update(guc=guc(), context=db.current_user_id.get(), scope=get_scope().get("user"))
        return Dataset.objects.count()

    assert tasks.run_as_tenant_in_worker(work, user.pk) == 1
    assert seen == {"guc": str(user.pk), "context": user.pk, "scope": user.pk}
    assert guc() == ""  # cleared, and the next task starts without a tenant
    assert db.current_user_id.get() is None
    assert get_scope() == {}
    with scopes_disabled():
        assert Dataset.objects.count() == 0  # no context = no rows, again


@pytest.mark.django_db(transaction=True)
def test_worker_path_survives_a_reconnect_mid_task(user: User, other_user: User) -> None:
    """A dropped connection must not silently un-scope a running task: it would then read
    nothing and report success over an empty database."""
    with acting_as(user):
        create_dataset_for(user, name="mine")
    with acting_as(other_user):
        create_dataset_for(other_user, name="theirs")

    def work(owner_id: uuid.UUID) -> int:
        connection.close()  # e.g. a database failover, or a task that closes it itself
        return Dataset.objects.count()

    assert tasks.run_as_tenant_in_worker(work, user.pk) == 1
    assert guc() == ""


@pytest.mark.django_db(transaction=True)
def test_pinned_shell_tenant_survives_a_reconnect(user: User) -> None:
    db.pin_session_tenant(user.pk)
    try:
        assert guc() == str(user.pk)
        connection.close()
        assert guc() == str(user.pk)  # re-applied by the connection_created receiver
    finally:
        db.unpin_session_tenant()
    assert guc() == ""
    connection.close()
    assert guc() == ""  # nothing is re-applied once unpinned


def test_tenant_task_rejects_a_malformed_owner_id() -> None:
    result = tasks.dataset_summary.delay("not-an-id")

    assert result.state == "FAILURE"
    assert isinstance(result.result, ValueError)


@pytest.mark.parametrize(
    ("break_it", "problem"),
    [
        (
            "ALTER TABLE datasets_dataset DISABLE ROW LEVEL SECURITY",
            "datasets_dataset: row-level security not enabled",
        ),
        (
            f"DROP POLICY {rls.POLICY} ON datasets_dataset; "
            f"CREATE POLICY {rls.POLICY} ON datasets_dataset TO app_admin "
            "USING (true) WITH CHECK (true)",
            f"datasets_dataset: policy '{rls.POLICY}' does not apply to role app_user",
        ),
        (
            f"DROP POLICY {rls.POLICY} ON datasets_dataset; "
            f"CREATE POLICY {rls.POLICY} ON datasets_dataset TO app_user USING (true)",
            f"datasets_dataset: policy '{rls.POLICY}' needs both USING and WITH CHECK",
        ),
        (
            "REVOKE ALL ON datasets_dataset FROM app_user",
            "datasets_dataset: role app_user lacks SELECT/INSERT/UPDATE/DELETE on the table",
        ),
    ],
    ids=["rls-disabled", "wrong-role", "no-with-check", "no-grant"],
)
@pytest.mark.cross_tenant
def test_verify_reports_every_kind_of_drift_and_sync_repairs_it(
    break_it: str, problem: str
) -> None:
    with connection.cursor() as cursor:
        cursor.execute(break_it)

    assert problem in rls.verify()
    rls.sync()
    assert rls.verify() == []


def test_verify_and_sync_handle_tables_that_do_not_exist_yet() -> None:
    assert rls.verify(tables=["future_table"]) == [
        "future_table: table missing (unapplied migration?)"
    ]


@pytest.mark.cross_tenant
def test_check_rls_applied_waits_for_pending_migrations(monkeypatch: pytest.MonkeyPatch) -> None:
    with connection.cursor() as cursor:
        cursor.execute(f"DROP POLICY {rls.POLICY} ON datasets_dataset")
    assert checks.check_rls_applied(None, ["default"])  # drift is reported ...

    monkeypatch.setattr(
        "django.db.migrations.executor.MigrationExecutor.migration_plan",
        lambda self, targets, clean_start=False: [("pending", False)],
    )
    assert checks.check_rls_applied(None, ["default"]) == []  # ... unless a migrate is due


@pytest.mark.cross_tenant
@pytest.mark.parametrize(
    ("role", "reason"),
    [
        ("app_admin", "role app_admin has BYPASSRLS"),
        ("app_migrator", "role app_migrator owns the tables"),
    ],
)
def test_connection_bypasses_rls_names_the_reason(role: str, reason: str) -> None:
    with connection.cursor() as cursor:
        # The test database's tables belong to the connecting superuser; give one to the
        # migrator (rolled back with the test) so "owns the tables" is observable.
        cursor.execute("ALTER TABLE datasets_dataset OWNER TO app_migrator")
        cursor.execute(f"SET ROLE {role}")
    try:
        assert rls.connection_bypasses_rls() == reason
        assert rls.current_role() == role
    finally:
        with connection.cursor() as cursor:
            cursor.execute("RESET ROLE")


def test_readiness_rls_check_reports_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> list[str]:
        raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(rls, "verify", boom)

    check = health.check_rls()
    assert (check.ok, check.detail) == (False, "RuntimeError: catalog unavailable")


def test_scope_with_none_is_not_a_backdoor(user: User) -> None:
    with acting_as(user):
        create_dataset_for(user, name="mine")
    with scope(user=None), pytest.raises(ScopeError, match="primary key"):
        Dataset.objects.count()


def test_owned_upload_path_needs_an_owned_instance_with_an_owner(user: User) -> None:
    from apps.core.models import owned_upload_path
    from apps.documents.models import Document

    with pytest.raises(TypeError, match="OwnedModel"):
        owned_upload_path(User(), "x.bin")
    with pytest.raises(NoTenantContext):
        owned_upload_path(Document(), "x.bin")
    with acting_as(user):
        assert owned_upload_path(Document(), "x.bin").startswith(f"documents/{user.pk}/")
        # The context also fills in the owner when a model instance is saved directly.
        document = Document(name="x.bin", size=1, file=ContentFile(b"x", name="x.bin"))
        document.save()
        assert document.owner == user
        assert str(document.file.name).startswith(f"documents/{user.pk}/")


def test_scrub_leaves_models_without_pii_alone(user: User) -> None:
    with acting_as(user):
        dataset = create_dataset_for(user, name="plain")
    scrubbed = scrub.scrub(dataset)
    assert isinstance(scrubbed, Dataset) and scrubbed.name == "plain"
    assert scrub.pii_fields(Dataset) == []


def test_middleware_warns_about_streaming_responses(user: User) -> None:
    from django.http import StreamingHttpResponse
    from structlog.testing import capture_logs

    def view(request: HttpRequest) -> StreamingHttpResponse:
        return StreamingHttpResponse(iter([b"chunk"]))

    request = RequestFactory().get(
        "/api/stream", headers={"Authorization": f"Bearer {issue_access_token(user)}"}
    )
    with capture_logs() as logs:
        response = middleware.TenantMiddleware(view)(request)

    assert response.streaming
    assert [(log["event"], log["log_level"]) for log in logs] == [
        ("tenant_streaming_response", "warning")
    ]


def test_bearer_scheme_is_case_insensitive_like_ninja(user: User) -> None:
    client = Client(headers={"Authorization": f"bearer {issue_access_token(user)}"})

    assert client.get("/api/auth/me").json()["username"] == "alice"


def test_bearer_wins_over_a_session_and_a_bad_bearer_is_still_401(
    client: Client, staff_user: User, other_user: User
) -> None:
    client.force_login(staff_user)

    as_bob = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {issue_access_token(other_user)}"}
    )
    assert as_bob.json()["username"] == "bob"  # the session never leaks into the API identity
    assert client.get("/api/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401


# --- invariants that live outside Python: roles, settings wiring, check registration -----------


def test_the_runtime_role_can_never_bypass_the_policies() -> None:
    """Invariant 1: `app_user` owns no owned table, is no superuser and has no BYPASSRLS —
    any of the three would make every policy a no-op (docker/postgres/10-roles.sh)."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolsuper, rolbypassrls, rolcreaterole FROM pg_roles WHERE rolname = %s",
            [rls.APP_ROLE],
        )
        attributes = cursor.fetchone()
        cursor.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
            "AND tablename = ANY(%s) AND tableowner = %s",
            [rls.tenant_tables(), rls.APP_ROLE],
        )
        owned = cursor.fetchall()

    assert attributes == (False, False, False)
    assert owned == []
    assert rls.current_role() == rls.APP_ROLE  # ...and this is the role the tests run as


def test_bulk_create_still_needs_an_owner_and_obeys_the_policy(
    user: User, other_user: User
) -> None:
    """`bulk_create` skips `save()`, so nothing fills in the owner — the policy catches that
    as well as a foreign owner: a NULL `owner_id` makes the WITH CHECK comparison NULL, which
    is not true, so the row is rejected before the NOT NULL constraint is even reached. Pass
    `owner=` explicitly when bulk-inserting."""
    ownerless = [Dataset(name="no owner")]
    foreign = [Dataset(name="foreign", owner=other_user)]
    with acting_as(user):
        for rows in (ownerless, foreign):
            with pytest.raises(DatabaseError, match="row-level security"), transaction.atomic():
                Dataset.objects.bulk_create(rows)

        Dataset.objects.bulk_create([Dataset(name="explicit", owner=user)])
        assert list(Dataset.objects.values_list("name", flat=True)) == ["explicit"]


def test_deleting_a_user_erases_exactly_that_tenant(user: User, other_user: User) -> None:
    """Erasure works from inside the tenant's own context: the cascade sees the rows and
    removes them. From another tenant's context those rows are invisible, so the cascade
    clears nothing and only the foreign key catches it — and because Django declares its
    constraints DEFERRABLE INITIALLY DEFERRED, that happens at COMMIT, far from the call.
    Account-deletion tooling has to run with cross-tenant credentials."""
    with acting_as(user):
        create_dataset_for(user, name="mine")
    with acting_as(other_user):
        create_dataset_for(other_user, name="theirs")

    with acting_as(other_user), pytest.raises(IntegrityError), transaction.atomic():
        User.objects.get(pk=user.pk).delete()  # collects nothing: RLS hides the datasets
        with connection.cursor() as cursor:
            cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")  # what COMMIT would do

    with acting_as(user), hard_delete():  # the cascade is a real delete (apps/core/tenants.py)
        User.objects.get(pk=user.pk).delete()
        assert Dataset.objects.count() == 0
    with acting_as(other_user):
        assert list(Dataset.objects.values_list("name", flat=True)) == ["theirs"]


def test_the_tenant_checks_are_registered_with_django() -> None:
    """`manage.py check` must run them — `apps.core.apps.CoreConfig.ready()` imports the module."""
    registered = {getattr(check, "__name__", "") for check in check_registry.registered_checks}

    assert {"check_tenant_models", "check_rls_applied"} <= registered
    assert run_checks(tags=[Tags.models]) == []


def test_settings_wire_the_isolation_layers_together() -> None:
    middleware_order = list(settings.MIDDLEWARE)
    tenant = middleware_order.index("apps.core.middleware.TenantMiddleware")

    # After AuthenticationMiddleware (which would overwrite request.user), before the logging
    # middleware and any view that reads owned data.
    assert tenant > middleware_order.index(
        "django.contrib.auth.middleware.AuthenticationMiddleware"
    )
    assert tenant < middleware_order.index("django_structlog.middlewares.RequestMiddleware")
    # The middleware owns the request transaction; Django's would start too late for SET LOCAL.
    assert settings.DATABASES["default"]["ATOMIC_REQUESTS"] is False
    assert "django_scopes" in settings.INSTALLED_APPS
    assert db.guc_name() == "app.user_id"
    # Cross-tenant work runs on its own queue, consumed only by the maintenance worker.
    assert settings.CELERY_TASK_ROUTES == {
        "apps.core.tasks.backup_database": {"queue": "maintenance"}
    }
    assert tasks.backup_database.name in settings.CELERY_TASK_ROUTES


# --- third round: the policy expression itself, privileges, quiet syncs ------------------------


def test_the_expected_expression_is_what_postgres_stores() -> None:
    """`verify()` compares expressions as strings, so this pins how Postgres renders ours —
    a release that renders it differently must break here, not in production."""
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_expr(polqual, polrelid), pg_get_expr(polwithcheck, polrelid) "
            "FROM pg_policy WHERE polname = %s LIMIT 1",
            [rls.POLICY],
        )
        using, with_check = cursor.fetchone()

    assert using == with_check == rls.expected_expression()
    assert db.guc_name() in rls.expected_expression()


@pytest.mark.cross_tenant
def test_verify_catches_a_policy_that_was_widened(user: User, other_user: User) -> None:
    """A policy can exist, apply to app_user and have both clauses while isolating nothing."""
    with connection.cursor() as cursor:
        cursor.execute(f"ALTER POLICY {rls.POLICY} ON datasets_dataset USING (true)")

    problems = rls.verify()

    assert len(problems) == 1
    assert "does not isolate tenants" in problems[0]
    assert rls.sync() == ["datasets_dataset"]
    assert rls.verify() == []


@pytest.mark.cross_tenant
def test_verify_catches_a_policy_left_over_from_another_guc(settings: Settings) -> None:
    settings.TENANT_GUC = "app.other_id"

    problems = rls.verify(tables=["datasets_dataset"])

    assert "does not isolate tenants" in problems[0]
    assert "app.other_id" in problems[0]  # what it should have been


@pytest.mark.cross_tenant
def test_verify_catches_a_read_only_grant() -> None:
    """Regression: `has_table_privilege(role, table, 'SELECT, INSERT, ...')` is OR, not AND —
    a read-only grant used to pass the check while every write 500s."""
    with connection.cursor() as cursor:
        cursor.execute(f"REVOKE INSERT, UPDATE, DELETE ON datasets_dataset FROM {rls.APP_ROLE}")

    assert rls.verify() == [
        f"datasets_dataset: role {rls.APP_ROLE} lacks SELECT/INSERT/UPDATE/DELETE on the table"
    ]
    rls.sync()
    assert rls.verify() == []


@pytest.mark.cross_tenant
def test_sync_changes_nothing_when_the_policies_are_healthy() -> None:
    """It runs on every container start; a healthy database must not see DDL (and its locks)."""
    assert rls.sync() == []
    assert rls.sync(tables=["future_table", "datasets_dataset"]) == []

    with connection.cursor() as cursor:
        cursor.execute(f"DROP POLICY {rls.POLICY} ON gallery_mediaitem")
    assert rls.sync() == ["gallery_mediaitem"]  # only the table that needed it
    assert rls.sync() == []


# --- erasing a tenant (apps/core/tenants.py) ---------------------------------------------------


def test_erasing_a_tenant_needs_cross_tenant_credentials(user: User) -> None:
    with pytest.raises(rls.CrossTenantAccessRequired, match="DB_ROLE=migrator"):
        tenants.tenant_summary(user)
    with pytest.raises(rls.CrossTenantAccessRequired):
        tenants.delete_tenant(user)


@pytest.mark.cross_tenant
def test_delete_tenant_removes_every_row_and_file_of_one_user(
    user: User, other_user: User, media_root: Path
) -> None:
    for owner in (user, other_user):
        with acting_as(owner):
            create_dataset_for(owner, name=f"{owner.username}s data")
            store_documents(
                owner, [SimpleUploadedFile("f.pdf", b"%PDF", content_type="application/pdf")]
            )
    assert len(list(media_root.rglob("*.pdf"))) == 2

    summary = tenants.tenant_summary(user)
    erasure = tenants.delete_tenant(user)

    # History and lineage are erased with the rows they describe, and counted in the preview.
    assert summary == {
        "core.Lineage": 0,
        "datasets.Dataset": 1,
        "datasets.DatasetEvent": 1,
        "datasets.DatasetTag": 0,
        "datasets.DatasetTagEvent": 0,
        "datasets.Tag": 0,
        "datasets.TagEvent": 0,
        "documents.Document": 1,
        "documents.DocumentEvent": 1,
        "gallery.MediaItem": 0,
        "gallery.MediaItemEvent": 0,
        "notes.Note": 0,
        "notes.NoteEvent": 0,
    }
    assert (erasure.username, erasure.rows, erasure.files) == ("alice", summary, 1)
    with scopes_disabled():
        assert not User.objects.filter(pk=user.pk).exists()
        assert Dataset.objects.count() == 1  # the other tenant is untouched
    assert [path.parent.parent.parent.name for path in media_root.rglob("*.pdf")] == [
        str(other_user.pk)
    ]


@pytest.mark.cross_tenant
def test_delete_tenant_keeps_the_files_when_the_rows_survive(
    user: User, media_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Files are deleted only after the database transaction commits — a rollback must not
    leave rows pointing at objects that are already gone."""
    with acting_as(user):
        store_documents(
            user, [SimpleUploadedFile("keep.pdf", b"%PDF", content_type="application/pdf")]
        )

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(User, "delete", boom)
    with pytest.raises(RuntimeError, match="database went away"):
        tenants.delete_tenant(user)

    assert len(list(media_root.rglob("*.pdf"))) == 1


@isolate_apps("apps.datasets", "apps.accounts")  # accounts: the owner FK must resolve
def test_long_app_names_still_get_a_valid_owner_index() -> None:
    """`Index.max_name_length` is 30. The abstract base must not pin a literal name pattern, or
    `manage.py newapp subscriptions --model Subscription` would die in its own
    `makemigrations` with models.E034 and leave the repo half-scaffolded."""

    class SubscriptionRenewalReminder(OwnedModel):
        class Meta(OwnedModel.Meta):
            app_label = "datasets"

    (index,) = SubscriptionRenewalReminder._meta.indexes
    index.set_name_with_model(SubscriptionRenewalReminder)

    assert index.fields == ["owner", "-created", "-id"]
    assert len(index.name) <= models.Index.max_name_length
    # models.E034 = "index name is longer than 30 characters". (fields.E300 is expected here:
    # the owner FK points at accounts.User, which the isolated registry does not contain.)
    model_errors = SubscriptionRenewalReminder.check()
    assert [error.id for error in model_errors if str(error.id).startswith("models.")] == []
