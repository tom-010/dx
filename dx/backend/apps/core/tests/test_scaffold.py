"""`manage.py startmodule` (apps/core/scaffold.py) on temporary copies of the project files."""

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


def test_module_spec_validates_names() -> None:
    spec = scaffold.module_spec("todo_items")
    assert (spec.model, spec.singular, spec.module_class) == ("TodoItem", "todo_item", "TodoItems")
    assert scaffold.module_spec("inventory", "Item").model == "Item"

    with pytest.raises(scaffold.ScaffoldError, match="snake_case"):
        scaffold.module_spec("Reports")
    with pytest.raises(scaffold.ScaffoldError, match="CamelCase"):
        scaffold.module_spec("reports", "report")


def test_render_module_writes_compilable_python(tmp_path: Path) -> None:
    spec = scaffold.module_spec("reports")

    files = scaffold.render_module(spec, tmp_path / "apps")

    names = sorted(str(f.relative_to(tmp_path / "apps" / "reports")) for f in files)
    assert names == [
        "__init__.py",
        "admin.py",
        "api.py",
        "apps.py",
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
        scaffold.render_module(spec, tmp_path / "apps")


def test_register_module_inserts_at_the_needles(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.py"
    api_file = tmp_path / "api.py"
    shutil.copy(BASE_DIR / "config" / "settings.py", settings_file)
    shutil.copy(BASE_DIR / "config" / "api.py", api_file)
    spec = scaffold.module_spec("reports")

    scaffold.register_module(spec, settings_file, api_file)

    settings_lines = settings_file.read_text().splitlines()
    app_index = settings_lines.index('    "apps.reports",')
    assert scaffold.SETTINGS_NEEDLE in settings_lines[app_index + 1]
    api_lines = api_file.read_text().splitlines()
    assert "from apps.reports.api import router as reports_router" in api_lines
    router_index = api_lines.index('api.add_router("/", reports_router)')
    assert scaffold.ROUTERS_NEEDLE in api_lines[router_index + 1]

    with pytest.raises(scaffold.ScaffoldError, match="already in INSTALLED_APPS"):
        scaffold.register_module(spec, settings_file, api_file)


def test_register_module_fails_without_needle(tmp_path: Path) -> None:
    settings_file = tmp_path / "settings.py"
    settings_file.write_text("INSTALLED_APPS = []\n")

    with pytest.raises(scaffold.ScaffoldError, match="needle"):
        scaffold.register_module(scaffold.module_spec("reports"), settings_file, tmp_path / "x")


def test_generated_tests_write_inside_a_tenant_context(tmp_path: Path) -> None:
    """Writes raise outside a tenant context, so the scaffold's own tests must open one —
    otherwise every new module starts with a red suite (CLAUDE.md "Multitenancy")."""
    scaffold.render_module(scaffold.module_spec("reports"), tmp_path / "apps")
    generated = (tmp_path / "apps" / "reports" / "tests" / "test_api.py").read_text()

    assert "from apps.core.testing import acting_as" in generated
    lines = generated.splitlines()
    calls = [i for i, line in enumerate(lines) if "Report.objects.create(" in line]

    assert len(calls) == 2
    for index in calls:
        preceding = "\n".join(lines[max(index - 2, 0) : index + 1])
        assert "acting_as(user)" in preceding, f"no tenant context: {lines[index].strip()}"
    assert "with acting_as(user), pytest.raises(HttpError):" in generated
