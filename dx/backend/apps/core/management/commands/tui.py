"""`manage.py tui` — a full-screen explorer for every management command (Textual).

    manage.py tui                  # type to filter, ↑↓ to pick, enter to run, esc to leave
    manage.py tui delete tenant    # ...opened on a query (every word goes in the filter box)
    manage.py tui --list           # plain table for pipes and scripts, no UI at all

Commands are how this project is operated and there are ~50 of them, ours mixed in with
Django's and the libraries'. `manage.py help` lists the names; this searches names *and*
descriptions and shows the real `--help` of whatever is highlighted.

An unfiltered list leads with the commands you ran last — `manage.py` records every invocation
(`apps/core/usage.py`) — and what you ran last breaks ties while filtering too.

**The one input is both the search box and the command line.** Enter on a highlighted command
puts its name in the box; type the arguments and options after it (`newapp reports --model
Report`) and enter runs exactly the line shown at the bottom of the screen. Escape clears it
again. A command that needs an argument is therefore never run without one.

**Running a command leaves the explorer.** The app returns the line it built, the process runs
it with the terminal to itself and exits with its exit code — so the output stays on screen,
`manage.py tui` composes with a shell the way any other command does, and nothing repaints
over what you just read.

The index, the ranking and the captured `--help` live in `apps/core/cli.py` (which is what the
tests exercise); this module is the screen.
"""

import shlex
import subprocess
import sys
from collections.abc import Container, Sequence

import djclick as click
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option

from apps.core.cli import GROUPS, CommandInfo, command_index, full_help, group_by_app, search
from apps.core.usage import recent_runs
from config.env import BASE_DIR

console = Console()

#: The command line a child process is started with — this interpreter, this project.
MANAGE = BASE_DIR / "manage.py"
#: How many recently used commands lead the list: enough for a working session, few enough that
#: the list is still the list of every command.
RECENT_SHOWN = 5
#: The heading that group sits under.
RECENT = "recently used"


def split_query(text: str, names: Container[str] = ()) -> tuple[str, list[str]]:
    """Split the one input into what filters the list and what runs as arguments.

        "del ten"                  → ("del ten", [])              still searching
        "hello_world dx --shout"   → ("hello_world", ["dx", …])   the first word is a command

    The split only happens once the first word *is* a command name; until then every word
    narrows the search (`cli.terms_score`), which is what makes a two-word query work at all.
    Unbalanced quotes are simply not arguments yet — someone is still typing.
    """
    query = text.strip()
    first, _, rest = query.partition(" ")
    if first not in names or not rest.strip():
        return query, []
    try:
        return first, shlex.split(rest)
    except ValueError:
        return first, []


#: What the app hands back: the command to run and its arguments, or None if you just left.
Selection = tuple[str, list[str]]


class Explorer(App[Selection | None]):
    """The screen: a filter box, the matches, and the highlighted command's own help.

    It does not run anything itself — `run()` returns the line to run (see the module docstring).
    """

    TITLE = "manage.py"

    #: The terminal's own palette and background, rather than a theme painted over it. The
    #: command palette (ctrl+p) switches to any of textual's if you want one.
    THEME = "ansi-dark"

    # `Header` and `Footer` dock themselves; anything else that docks top fights them for the
    # row and wins silently.
    CSS = """
    #layout { height: 1fr; }
    #matches { width: 40%; height: 1fr; }
    #help { width: 1fr; border-left: solid $primary; padding: 0 1; }
    #runline { height: 1; padding: 0 1; }
    """

    BINDINGS = [
        # priority: the focused Input binds enter itself (submit), and this has to win.
        Binding("enter", "run_command", "choose / run", priority=True),
        Binding("escape", "leave", "back", priority=True),
        Binding("down", "move(1)", "next", show=False),
        Binding("up", "move(-1)", "previous", show=False),
        Binding("pagedown", "move(10)", show=False),
        Binding("pageup", "move(-10)", show=False),
        Binding("ctrl+f", "toggle_descriptions", "search descriptions"),
    ]

    def __init__(
        self,
        commands: list[CommandInfo],
        query: str = "",
        *,
        descriptions: bool = False,
        recent: Sequence[str] = (),
    ) -> None:
        super().__init__()
        self.commands = commands
        #: Command names, most recently run first (`apps/core/usage.py`).
        self.recent = list(recent)
        self.initial_query = query
        #: Search the descriptions as well as the names (ctrl+f).
        self.descriptions = descriptions
        #: One entry per row of the list, `None` where the row is an app heading.
        self.rows: list[CommandInfo | None] = []
        #: What counts as "the first word names a command" (see `split_query`).
        self.names = {command.name for command in commands}
        #: `--help` per (command, pane width) — capturing it means running the command.
        self.helps: dict[tuple[str, int], str] = {}

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="filter… then arguments: hello_world dx --shout", id="query")
        with Horizontal(id="layout"):
            yield OptionList(id="matches")
            with VerticalScroll(id="help"):
                yield Static(id="detail")
        yield Static(id="runline")
        yield Footer()

    def on_mount(self) -> None:
        self.theme = self.THEME
        self.show_mode()
        box = self.query_one("#query", Input)
        box.value = self.initial_query
        box.focus()
        self.refresh_matches()

    # --- what is shown ------------------------------------------------------------------------

    def refresh_matches(self) -> None:
        query, _ = split_query(self.query_one("#query", Input).value, self.names)
        found = search(self.commands, query, descriptions=self.descriptions, recent=self.recent)

        # Grouped under the app that ships them; the heading is a disabled option, so textual's
        # own navigation steps over it and `rows` keeps the list and the widget aligned.
        self.rows = []
        options = []
        for app, commands in self.groups(found, query):
            options.append(Option(Text(app, style="dim italic"), disabled=True))
            self.rows.append(None)
            for command in commands:
                options.append(Option(_row(command)))
                self.rows.append(command)

        widget = self.query_one("#matches", OptionList)
        widget.clear_options()
        widget.add_options(options)
        widget.highlighted = 1 if found else None
        self.show_detail()
        self.show_runline()

    def groups(self, found: list[CommandInfo], query: str) -> list[tuple[str, list[CommandInfo]]]:
        """The list as it is shown: what you ran last, then every command under its app.

        The recent group only leads an *unfiltered* list — once you are searching, the ranking
        (which already favours what you ran last) is the better order. Its members are taken out
        of their app group, so no command appears twice.
        """
        by_app = group_by_app(found)
        if query.strip() or not self.recent:
            return by_app

        known = {command.name: command for command in found}
        recent = [known[name] for name in self.recent if name in known][:RECENT_SHOWN]
        if not recent:
            return by_app
        used = {command.name for command in recent}
        rest = [(app, [c for c in commands if c.name not in used]) for app, commands in by_app]
        return [(RECENT, recent), *[(app, commands) for app, commands in rest if commands]]

    def armed(self) -> bool:
        """Is the input a command line now rather than a search?

        True once its first word is an exact command name *and* a space follows — which is what
        the first enter does. Until then every word is part of the search.
        """
        value = self.query_one("#query", Input).value
        return " " in value and value.split(" ", 1)[0] in self.names

    def show_runline(self) -> None:
        """The bottom line: what enter does right now, spelled out."""
        line = self.query_one("#runline", Static)
        name, arguments = split_query(self.query_one("#query", Input).value, self.names)
        if self.armed():
            printed = " ".join(["manage.py", name, *arguments])
            line.update(
                Text.assemble(
                    "▶ ", (printed, "bold"), ("   enter runs it and leaves · esc clears", "dim")
                )
            )
            return
        chosen = self.highlighted_command()
        hint = (
            f"enter picks {chosen.name} — then type its arguments"
            if chosen is not None
            else "nothing to run"
        )
        line.update(Text(hint, style="dim"))

    def show_detail(self) -> None:
        command = self.highlighted_command()
        detail = self.query_one("#detail", Static)
        if command is None:
            detail.update(Text("no command matches", style="dim"))
            return
        detail.update(self.detail_of(command))

    def detail_of(self, command: CommandInfo) -> Text:
        """What enter would run, then the command's own `--help`.

        Assembled as a `Text` rather than markup: help output is full of `[0<=x<=3]` and rich
        would read that as a style tag.
        """
        _, arguments = split_query(self.query_one("#query", Input).value, self.names)
        line = " ".join(["manage.py", command.name, *arguments])
        detail = Text.assemble((line, "bold cyan"), (f"  ({command.app})\n\n", "dim"))
        if not command.loaded:
            return detail.append_text(Text(command.help, style="red"))
        # Keyed by width as well: a resize changes where click wraps.
        width = self.query_one("#help", VerticalScroll).content_size.width
        key = (command.name, width)
        self.helps.setdefault(key, full_help(command.name, width=width or 78))
        return detail.append_text(Text(self.helps[key]))

    def highlighted_command(self) -> CommandInfo | None:
        highlighted = self.query_one("#matches", OptionList).highlighted
        if highlighted is None or highlighted >= len(self.rows):
            return None
        return self.rows[highlighted]  # None on an app heading

    # --- what it does -------------------------------------------------------------------------

    def on_input_changed(self, event: Input.Changed) -> None:
        self.refresh_matches()

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        self.show_detail()
        self.show_runline()

    def action_move(self, delta: int) -> None:
        # Through the widget's own cursor actions, which step over the disabled headings.
        options = self.query_one("#matches", OptionList)
        step = options.action_cursor_down if delta > 0 else options.action_cursor_up
        for _ in range(abs(delta)):
            step()

    def action_toggle_descriptions(self) -> None:
        """Widen the search to what each command is *for*, not just its name."""
        self.descriptions = not self.descriptions
        self.show_mode()
        self.refresh_matches()

    def show_mode(self) -> None:
        self.sub_title = (
            "searching names + descriptions" if self.descriptions else "searching names"
        )

    def action_run_command(self) -> None:
        """First enter picks the highlighted command, the second leaves and runs it.

        The two steps are the point: `manage.py newapp` without its NAME just fails its own
        usage check, and there would be nowhere to type one. What runs is always the line the
        bottom of the screen is showing.
        """
        if not self.armed():
            command = self.highlighted_command()
            if command is None or not command.loaded:
                self.bell()
                return
            box = self.query_one("#query", Input)
            box.value = f"{command.name} "  # the trailing space is what "armed" means
            box.cursor_position = len(box.value)
            return

        # Hand the line back and close the app: the command gets the terminal to itself, and
        # what it prints is the last thing on the screen.
        self.exit(split_query(self.query_one("#query", Input).value, self.names))

    def action_leave(self) -> None:
        """Escape leaves the command line first, the explorer second.

        Not `action_back`: textual's `App` already has one, and it is a coroutine.
        """
        if self.armed():
            self.query_one("#query", Input).value = ""
            return
        self.exit()


def _row(command: CommandInfo) -> Text:
    """One entry in the list: the name, and under it what the command is for.

    Plain text, not bold: with every row shouting, the highlighted one no longer stands out.
    """
    name = Text(command.name, style="" if command.loaded else "red")
    return name.append_text(Text(f"\n{command.help}", style="dim"))


def run_command(name: str, arguments: list[str]) -> int:
    """Run `manage.py <name>` as a child process holding the terminal; returns its exit code.

    A child, not `execute_from_command_line` in this process, for two reasons that both make
    the in-process version useless here: the explorer runs inside an asyncio event loop, and
    Django refuses synchronous database access from one (`SynchronousOnlyOperation` — which is
    every command worth running), and a child gets a real stdin, so a command that prompts, or
    opens a shell, behaves exactly as it does outside the explorer. Its `sys.exit` is its own,
    too.
    """
    console.rule(f"[dim]manage.py {name} {shlex.join(arguments)}".rstrip() + "[/]")
    finished = subprocess.run([sys.executable, str(MANAGE), name, *arguments], cwd=MANAGE.parent)
    if finished.returncode:
        console.print(f"[red]exit code {finished.returncode}[/]")
    return finished.returncode


def print_table(commands: list[CommandInfo], query: str) -> None:
    """The index as a table: grouped when it is the whole list, ranked when it is a search."""
    if not commands:
        console.print(f"[yellow]no command matches {query!r}[/]")
        return
    if query.strip():
        console.print(_table(f"matches for {query!r}", commands))
        return
    for group in GROUPS:
        rows = [command for command in commands if command.group == group]
        if rows:
            console.print(_table(f"{group} commands", rows))


def _table(title: str, commands: list[CommandInfo]) -> Table:
    table = Table(title=title, title_justify="left", box=box.SIMPLE, pad_edge=False)
    table.add_column("manage.py", style="bold cyan", no_wrap=True)
    table.add_column("")
    for command in commands:
        table.add_row(command.name, command.help if command.loaded else f"[red]{command.help}[/]")
    return table


@click.command()
@click.argument("query", nargs=-1)
@click.option("--list", "as_list", is_flag=True, help="Print the table and exit, no UI.")
@click.option(
    "--descriptions",
    "-d",
    is_flag=True,
    help="Search what commands are for, not just their names (ctrl+f in the UI).",
)
def command(query: tuple[str, ...], as_list: bool, descriptions: bool) -> None:
    """Explore the management commands: find one, read its help, run it."""
    commands = command_index()
    # Every word goes in the filter box: `manage.py tui delete tenant` opens on "delete tenant".
    text = " ".join(query)
    recent = recent_runs()
    # A pipe gets the table: `manage.py tui | grep tenant` should say something useful, and a
    # full-screen app needs a terminal to be full-screen in.
    if as_list or not (sys.stdin.isatty() and sys.stdout.isatty()):
        print_table(search(commands, text, descriptions=descriptions, recent=recent), text)
        return

    chosen = Explorer(commands, text, descriptions=descriptions, recent=recent).run()
    if chosen is None:
        return  # left without picking anything
    # The app is gone by now: the command has the terminal, and its exit code becomes ours.
    sys.exit(run_command(*chosen))
