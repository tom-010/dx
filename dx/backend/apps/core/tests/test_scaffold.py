"""`manage.py newapp` (apps/core/scaffold.py) on temporary copies of the project files."""

import shutil
from pathlib import Path

import pytest

from apps.core import scaffold
from config.env import BASE_DIR


def test_default_model_name_singularizes() -> None:
    assert scaffold.default_model_name("reports") == "Report"
    assert scaffold.default_model_name("todo_items") == "TodoItem"
    assert scaffold.default_model_name("categories") == "Category"
    assert scaffold.default_model_name("address") == "Address"


def test_app_spec_validates_names() -> None:
    spec = scaffold.app_spec("todo_items")
    assert (spec.model, spec.singular) == ("TodoItem", "todo_item")
    assert scaffold.app_spec("inventory", "Item").model == "Item"

    with pytest.raises(scaffold.ScaffoldError, match="snake_case"):
        scaffold.app_spec("Reports")
    with pytest.raises(scaffold.ScaffoldError, match="CamelCase"):
        scaffold.app_spec("reports", "report")


def test_render_app_writes_compilable_python(tmp_path: Path) -> None:
    spec = scaffold.app_spec("reports")

    files = scaffold.render_app(spec, tmp_path / "apps")

    names = sorted(str(f.relative_to(tmp_path / "apps" / "reports")) for f in files)
    assert names == [
        "__init__.py",
        "admin.py",
        "api.py",
        # Every app is ready to hold commands; see .claude/rules/management-commands.md.
        "management/__init__.py",
        "management/commands/__init__.py",
        "migrations/__init__.py",
        "models.py",
        "tests/__init__.py",
        "tests/test_api.py",
    ]
    for file in files:
        source = file.read_text()
        assert "${" not in source, f"unrendered placeholder in {file}"
        compile(source, str(file), "exec")
    api = (tmp_path / "apps" / "reports" / "api.py").read_text()
    assert 'Router(tags=["reports"])' in api
    assert '@router.get("/reports/{report_id}"' in api
    assert "class Report(OwnedModel)" in (tmp_path / "apps" / "reports" / "models.py").read_text()

    with pytest.raises(scaffold.ScaffoldError, match="already exists"):
        scaffold.render_app(spec, tmp_path / "apps")


def test_register_app_inserts_at_the_needles(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.py"
    api_file = tmp_path / "api.py"
    shutil.copy(BASE_DIR / "config" / "settings.py", settings_file)
    shutil.copy(BASE_DIR / "config" / "api.py", api_file)
    spec = scaffold.app_spec("reports")

    scaffold.register_app(spec, settings_file, api_file)

    settings_lines = settings_file.read_text().splitlines()
    app_index = settings_lines.index('    "apps.reports",')
    assert scaffold.SETTINGS_NEEDLE in settings_lines[app_index + 1]
    api_lines = api_file.read_text().splitlines()
    assert "from apps.reports.api import router as reports_router" in api_lines
    router_index = api_lines.index('api.add_router("/", reports_router)')
    assert scaffold.ROUTERS_NEEDLE in api_lines[router_index + 1]

    with pytest.raises(scaffold.ScaffoldError, match="already in INSTALLED_APPS"):
        scaffold.register_app(spec, settings_file, api_file)


def test_register_app_fails_without_needle(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.py"
    settings_file.write_text("INSTALLED_APPS = []\n")

    with pytest.raises(scaffold.ScaffoldError, match="needle"):
        scaffold.register_app(scaffold.app_spec("reports"), settings_file, tmp_path / "x")


def test_generated_tests_write_inside_a_tenant_context(tmp_path: Path) -> None:
    """Writes raise outside a tenant context, so the scaffold's own tests must open one —
    otherwise every new app starts with a red suite (CLAUDE.md "Multitenancy")."""
    scaffold.render_app(scaffold.app_spec("reports"), tmp_path / "apps")
    generated = (tmp_path / "apps" / "reports" / "tests" / "test_api.py").read_text()

    assert "from apps.core.testing import acting_as" in generated
    lines = generated.splitlines()
    calls = [i for i, line in enumerate(lines) if "Report.objects.create(" in line]

    assert len(calls) == 2
    for index in calls:
        preceding = "\n".join(lines[max(index - 2, 0) : index + 1])
        assert "acting_as(user)" in preceding, f"no tenant context: {lines[index].strip()}"
    assert "with acting_as(user), pytest.raises(HttpError):" in generated


def test_render_command_writes_a_runnable_command(tmp_path: Path) -> None:
    app_dir = tmp_path / "apps" / "notes"

    written = scaffold.render_command("purge_old", app_dir)

    assert written == app_dir / "management" / "commands" / "purge_old.py"
    source = written.read_text()
    assert "${" not in source, "unrendered placeholder"
    compile(source, str(written), "exec")
    assert "def command(" in source
    # Commands are database tasks: not tenant-filtered by default (.claude/rules).
    assert "with all_tenants():" in source
    # The package files exist even in an app that lost them.
    assert (app_dir / "management" / "__init__.py").exists()
    assert (app_dir / "management" / "commands" / "__init__.py").exists()

    with pytest.raises(scaffold.ScaffoldError, match="already exists"):
        scaffold.render_command("purge_old", app_dir)
    with pytest.raises(scaffold.ScaffoldError, match="snake_case"):
        scaffold.render_command("PurgeOld", app_dir)


def test_registering_and_deleting_an_app_leave_the_config_as_it_was(tmp_path: Path) -> None:
    """`deleteapp` is the exact opposite of `newapp`: nothing of it stays behind."""
    settings_file = tmp_path / "settings.py"
    api_file = tmp_path / "api.py"
    shutil.copy(BASE_DIR / "config" / "settings.py", settings_file)
    shutil.copy(BASE_DIR / "config" / "api.py", api_file)
    before = (settings_file.read_text(), api_file.read_text())
    spec = scaffold.app_spec("reports")
    scaffold.render_app(spec, tmp_path / "apps")
    scaffold.register_app(spec, settings_file, api_file)

    assert scaffold.unregister_app("reports", settings_file, api_file) == []
    assert scaffold.remove_app("reports", tmp_path / "apps") == 9

    assert (settings_file.read_text(), api_file.read_text()) == before
    assert not (tmp_path / "apps" / "reports").exists()


def test_unregister_app_leaves_a_similarly_named_app_alone(tmp_path: Path) -> None:
    """`apps.reports` is a prefix of `apps.reports_archive`: whole lines are matched, never
    substrings, or deleting one app would half-unregister the other."""
    settings_file = tmp_path / "settings.py"
    api_file = tmp_path / "api.py"
    shutil.copy(BASE_DIR / "config" / "settings.py", settings_file)
    shutil.copy(BASE_DIR / "config" / "api.py", api_file)
    for name in ("reports", "reports_archive"):
        scaffold.register_app(scaffold.app_spec(name), settings_file, api_file)

    scaffold.unregister_app("reports", settings_file, api_file)

    assert '"apps.reports_archive",' in settings_file.read_text()
    assert '"apps.reports",' not in settings_file.read_text()
    assert "reports_archive_router" in api_file.read_text()
    assert "as reports_router" not in api_file.read_text()


def test_unregister_app_reports_what_it_could_not_find(tmp_path: Path) -> None:
    """A registration somebody has edited by hand is named, not guessed at."""
    settings_file = tmp_path / "settings.py"
    api_file = tmp_path / "api.py"
    settings_file.write_text('INSTALLED_APPS = [\n    "apps.reports",\n]\n')
    api_file.write_text("# nothing registered here\n")

    missing = scaffold.unregister_app("reports", settings_file, api_file)

    assert missing == [
        "from apps.reports.api import router as reports_router",
        'api.add_router("/", reports_router)',
    ]
    assert '"apps.reports",' not in settings_file.read_text()


def test_remove_app_refuses_what_is_not_there(tmp_path: Path) -> None:
    with pytest.raises(scaffold.ScaffoldError, match="does not exist"):
        scaffold.remove_app("reports", tmp_path / "apps")
    with pytest.raises(scaffold.ScaffoldError, match="snake_case"):
        scaffold.remove_app("Reports", tmp_path / "apps")
