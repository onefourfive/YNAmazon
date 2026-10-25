"""Yes/no prompts, optionally answered with a single key press."""

import sys

import click
from rich.console import Console
from rich.prompt import Confirm

_one_key = False


def set_one_key(enabled: bool) -> None:
    """Turns single-key answers on or off for all prompts."""
    global _one_key
    _one_key = enabled


def one_key_enabled() -> bool:
    """Returns True if prompts accept a single y/n key press."""
    return _one_key


def confirm(question: str, console: Console | None = None) -> bool:
    """Asks a yes/no question.

    With single-key answers on, pressing y or n answers immediately, without Enter. Other keys are ignored.
    When input isn't a terminal (for example, piped answers), the normal prompt is used so that each answer
    is still read from a line of input.

    Raises:
        KeyboardInterrupt: If Ctrl-C is pressed.
        EOFError: If input ends (Ctrl-D).
    """
    if not _one_key or not sys.stdin.isatty():
        return Confirm.ask(question, console=console)

    console = console or Console()
    console.print(f"{question} [prompt.choices]\\[y/n][/]: ", end="")
    while True:
        key = click.getchar().lower()
        if key in ("y", "n"):
            console.print(key)
            return key == "y"
