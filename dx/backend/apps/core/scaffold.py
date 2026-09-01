"""Scaffolding for new feature apps (`manage.py newapp <name>`).

Templates live in `backend/scaffold/app/` (`*.py.tmpl`, `string.Template` placeholders:
`${name}` = app name (`reports`), `${plural}` = list function suffix (`reports`),
`${model}` = singular snake_case (`report`), `${Model}` = CamelCase (`Report`)). They are copied
to `apps/<name>/`, then the app is registered at the `# needle:` comments in
`config/settings.py` (INSTALLED_APPS) and `config/api.py`.
Everything here is pure file work so it can be tested on a temporary copy.
"""

import re
from dataclasses import dataclass
from pathlib import Path
from string import Template

from config.env import BASE_DIR

TEMPLATE_DIR = BASE_DIR / "scaffold" / "app"
SETTINGS_NEEDLE = "# needle: installed-apps"
ROUTERS_NEEDLE = "# needle: routers"

_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
_MODEL = re.compile(r"^[A-Z][A-Za-z0-9]*$")


class ScaffoldError(Exception):
    """The message is meant for the developer running the command."""


@dataclass(frozen=True)
class AppSpec:
    name: str  # app name, snake_case plural: "reports"
    model: str  # model class, CamelCase singular: "Report"

    @property
    def singular(self) -> str:
        return _snake(self.model)

    def substitutions(self) -> dict[str, str]:
        return {
            "name": self.name,
            "plural": self.name,
            "model": self.singular,
            "Model": self.model,
        }


def _camel(snake: str) -> str:
    return "".join(part.capitalize() for part in snake.split("_"))


def _snake(camel: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


def default_model_name(name: str) -> str:
    """`reports` → `Report`, `todo_items` → `TodoItem`, `categories` → `Category`.
    A heuristic; `--model` overrides it."""
    if name.endswith("ies"):
        singular = name[:-3] + "y"
    elif name.endswith("s") and not name.endswith("ss"):
        singular = name[:-1]
    else:
        singular = name
    return _camel(singular)


def app_spec(name: str, model: str | None = None) -> AppSpec:
    if not _NAME.match(name):
        raise ScaffoldError("app name must be snake_case (letters, digits, _), e.g. reports")
    model = model or default_model_name(name)
    if not _MODEL.match(model):
        raise ScaffoldError("model name must be CamelCase, e.g. Report")
    return AppSpec(name=name, model=model)


def render_app(spec: AppSpec, apps_dir: Path, templates: Path = TEMPLATE_DIR) -> list[Path]:
    """Write `apps/<name>/` from the templates; returns the created files."""
    target = apps_dir / spec.name
    if target.exists():
        raise ScaffoldError(f"{target} already exists")
    written = []
    for template_file in sorted(templates.rglob("*.tmpl")):
        relative = template_file.relative_to(templates).with_suffix("")  # strip .tmpl
        content = Template(template_file.read_text()).substitute(spec.substitutions())
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content)
        written.append(destination)
    return written


def insert_before_needle(path: Path, needle: str, lines: list[str]) -> None:
    """Insert `lines` (already indented) directly above the line containing `needle`."""
    content = path.read_text().splitlines(keepends=True)
    index = next((i for i, line in enumerate(content) if needle in line), None)
    if index is None:
        raise ScaffoldError(f"{path} has no '{needle}' marker; register the app by hand")
    content[index:index] = [line if line.endswith("\n") else line + "\n" for line in lines]
    path.write_text("".join(content))


def insert_after_last_match(path: Path, pattern: str, line: str) -> None:
    content = path.read_text().splitlines(keepends=True)
    matches = [i for i, existing in enumerate(content) if re.match(pattern, existing)]
    if not matches:
        raise ScaffoldError(f"{path}: nothing matches {pattern!r}")
    content.insert(matches[-1] + 1, line if line.endswith("\n") else line + "\n")
    path.write_text("".join(content))


def register_app(spec: AppSpec, settings_file: Path, api_file: Path) -> None:
    """Add the app to INSTALLED_APPS and mount its router (at the needle comments)."""
    app_line = f'    "apps.{spec.name}",'
    if app_line.strip() in settings_file.read_text():
        raise ScaffoldError(f"apps.{spec.name} is already in INSTALLED_APPS")
    insert_before_needle(settings_file, SETTINGS_NEEDLE, [app_line])
    insert_after_last_match(
        api_file,
        r"^from apps\.\w+\.api import router as \w+_router$",
        f"from apps.{spec.name}.api import router as {spec.name}_router",
    )
    insert_before_needle(api_file, ROUTERS_NEEDLE, [f'api.add_router("/", {spec.name}_router)'])
