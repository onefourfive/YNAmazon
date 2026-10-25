"""Yes/no prompts, optionally answered with a single key press."""

import os
import signal
import sys

import click
from rich import get_console
from rich.console import Console
from rich.prompt import Confirm

_one_key = False

# Keys that normally send a signal. Single-key prompts read the terminal in raw mode, where the terminal
# doesn't send these itself, so they're sent here to keep Ctrl-Z (suspend) and Ctrl-\ (quit) working.
_SIGNAL_KEYS = {
    "\x1a": getattr(signal, "SIGTSTP", None),  # Ctrl-Z; not available on Windows
    "\x1c": getattr(signal, "SIGQUIT", None),  # Ctrl-\; not available on Windows
}


def set_one_key(enabled: bool) -> None:
    """Turns single-key answers on or off for all prompts."""
    global _one_key
    _one_key = enabled


def confirm(question: str, console: Console | None = None) -> bool:
    """Asks a yes/no question.

    With single-key answers on, pressing y or n answers immediately, without Enter. Other keys are ignored.
    When input isn't a terminal (for example, piped answers), the normal prompt is used so that each answer
    is still read from a line of input.

    Raises:
        KeyboardInterrupt: If Ctrl-C is pressed.
        EOFError: If input ends (Ctrl-D). The CLI reports both as "Aborted!".
    """
    if not _one_key or not sys.stdin.isatty():
        return Confirm.ask(question, console=console)

    console = console or get_console()
    prompt = f"{question} [prompt.choices]\\[y/n][/]: "
    console.print(prompt, end="")
    while True:
        # One read can return several characters (a paste, or keys pressed quickly), so look at each one
        for key in click.getchar().lower():
            if key in ("y", "n"):
                console.print(key)
                return key == "y"
            signal_number = _SIGNAL_KEYS.get(key)
            if signal_number is not None:
                os.kill(os.getpid(), signal_number)
                # Show the prompt again after resuming from Ctrl-Z
                console.print()
                console.print(prompt, end="")
                break
