"""Scaffolding for new feature apps (`manage.py newapp <name>`).

Templates live in `backend/scaffold/app/` (`*.py.tmpl`, `string.Template` placeholders:
`${name}` = app name (`reports`), `${plural}` = list function suffix (`reports`),
`${model}` = singular snake_case (`report`), `${Model}` = CamelCase (`Report`)). They are copied
to `apps/<name>/`, then the app is registered at the `# needle:` comments in
`config/settings.py` (INSTALLED_APPS) and `config/api.py`.
Everything here is pure file work so it can be tested on a temporary copy.
"""

import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from string import Template

from config.env import BASE_DIR

TEMPLATE_DIR = BASE_DIR / "scaffold" / "app"
COMMAND_TEMPLATE = BASE_DIR / "scaffold" / "command.py.tmpl"
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


def render_command(name: str, app_dir: Path, template: Path = COMMAND_TEMPLATE) -> Path:
    """Write `<app>/management/commands/<name>.py` from the command template.

    The directory exists in every app already (and `newapp` scaffolds it), but it is created
    here too: an app that lost it should get a command, not an error.
    """
    if not _NAME.match(name):
        raise ScaffoldError("command name must be snake_case (letters, digits, _), e.g. purge_old")
    commands = app_dir / "management" / "commands"
    destination = commands / f"{name}.py"
    if destination.exists():
        raise ScaffoldError(f"{destination} already exists")
    commands.mkdir(parents=True, exist_ok=True)
    for package in (commands.parent, commands):
        (package / "__init__.py").touch()
    destination.write_text(Template(template.read_text()).substitute(name=name))
    return destination


# --- Removing one again (`manage.py deleteapp`) --------------------------------------------------


def registration_lines(name: str) -> dict[str, list[str]]:
    """The lines `register_app` writes, per file key ("settings" / "api")."""
    return {
        "settings": [f'"apps.{name}",'],
        "api": [
            f"from apps.{name}.api import router as {name}_router",
            f'api.add_router("/", {name}_router)',
        ],
    }


def unregister_app(name: str, settings_file: Path, api_file: Path) -> list[str]:
    """Undo `register_app`; returns the lines it expected but did not find.

    Whole lines are matched, never substrings: `apps.reports` is a prefix of
    `apps.reports_archive`, and half-unregistering the wrong app is worse than doing nothing. A
    line somebody has since edited is reported back rather than guessed at.
    """
    wanted = registration_lines(name)
    missing = []
    for path, lines in ((settings_file, wanted["settings"]), (api_file, wanted["api"])):
        kept, removed = [], set()
        for line in path.read_text().splitlines(keepends=True):
            if line.strip() in lines:
                removed.add(line.strip())
            else:
                kept.append(line)
        path.write_text("".join(kept))
        missing += [line for line in lines if line not in removed]
    return missing


def remove_app(name: str, apps_dir: Path) -> int:
    """Delete `apps/<name>/`; returns how many files went with it."""
    if not _NAME.match(name):
        raise ScaffoldError("app name must be snake_case (letters, digits, _), e.g. reports")
    target = apps_dir / name
    if not target.is_dir():
        raise ScaffoldError(f"{target} does not exist")
    files = sum(1 for path in target.rglob("*") if path.is_file())
    shutil.rmtree(target)
    return files
