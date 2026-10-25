"""Tests for y/n prompts and --one-key."""

from unittest.mock import Mock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from ynamazon import confirm as confirm_module
from ynamazon.confirm import confirm, set_one_key


@pytest.fixture(autouse=True)
def reset_one_key():
    """Keep the single-key mode from leaking between tests."""
    set_one_key(False)
    yield
    set_one_key(False)


@pytest.fixture
def one_key():
    """Turn single-key answers on with a terminal for stdin."""
    set_one_key(True)
    with patch("ynamazon.confirm.sys.stdin") as stdin:
        stdin.isatty.return_value = True
        yield


def _console() -> Console:
    return Console(file=Mock(), force_terminal=False)


@pytest.mark.parametrize(
    ("keys", "expected"), [(["y"], True), (["Y"], True), (["n"], False), (["N"], False)]
)
def test_one_key_answers_without_enter(one_key, keys, expected):
    with (
        patch("ynamazon.confirm.click.getchar", side_effect=keys) as getchar,
        patch("ynamazon.confirm.Confirm.ask") as ask,
    ):
        assert confirm("Update?", console=_console()) is expected

    assert getchar.call_count == 1
    ask.assert_not_called()


def test_one_key_ignores_other_keys(one_key):
    with patch("ynamazon.confirm.click.getchar", side_effect=["x", "\r", " ", "n"]) as getchar:
        assert confirm("Update?", console=_console()) is False
    assert getchar.call_count == 4


@pytest.mark.parametrize(
    ("chunk", "expected"), [("yy", True), ("y\r", True), ("\rn", False), ("xN", False)]
)
def test_one_key_handles_several_characters_in_one_read(one_key, chunk, expected):
    with patch("ynamazon.confirm.click.getchar", return_value=chunk) as getchar:
        assert confirm("Update?", console=_console()) is expected
    assert getchar.call_count == 1


@pytest.mark.parametrize(("key", "signal_name"), [("\x1a", "SIGTSTP"), ("\x1c", "SIGQUIT")])
def test_one_key_keeps_suspend_and_quit_keys_working(one_key, key, signal_name):
    import signal

    console = Console(record=True, force_terminal=False, width=80)
    with (
        patch("ynamazon.confirm.click.getchar", side_effect=[key, "y"]),
        patch("ynamazon.confirm.os.kill") as kill,
    ):
        assert confirm("Update?", console=console) is True

    kill.assert_called_once()
    assert kill.call_args.args[1] == getattr(signal, signal_name)
    # The prompt is shown again after resuming
    assert console.export_text() == "Update? [y/n]: \nUpdate? [y/n]: y\n"


def test_one_key_ctrl_c_still_interrupts(one_key):
    with (
        patch("ynamazon.confirm.click.getchar", side_effect=KeyboardInterrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        confirm("Update?", console=_console())


def test_one_key_echoes_answer(one_key):
    console = Console(record=True, force_terminal=False, width=80)
    with patch("ynamazon.confirm.click.getchar", return_value="y"):
        confirm("Update?", console=console)
    assert console.export_text() == "Update? [y/n]: y\n"


def test_default_uses_normal_prompt():
    with (
        patch("ynamazon.confirm.Confirm.ask", return_value=True) as ask,
        patch("ynamazon.confirm.click.getchar") as getchar,
    ):
        assert confirm("Update?") is True
    ask.assert_called_once()
    getchar.assert_not_called()


def test_one_key_falls_back_to_normal_prompt_when_input_is_piped():
    set_one_key(True)
    with (
        patch("ynamazon.confirm.sys.stdin") as stdin,
        patch("ynamazon.confirm.Confirm.ask", return_value=False) as ask,
        patch("ynamazon.confirm.click.getchar") as getchar,
    ):
        stdin.isatty.return_value = False
        assert confirm("Update?") is False
    ask.assert_called_once()
    getchar.assert_not_called()


def _one_key_seen_by_process(args: list[str], argv: list[str] | None = None) -> bool:
    """Runs the CLI and returns whether single-key mode was on when transactions were processed."""
    from ynamazon.cli.cli import cli

    seen = {}
    with (
        patch(
            "ynamazon.cli.cli.process_transactions",
            side_effect=lambda **_: seen.update(on=confirm_module._one_key),
        ),
        patch("sys.argv", argv or ["yna", *args]),
    ):
        result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    return seen["on"]


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["ynamazon", "--one-key"], True),
        (["--one-key", "ynamazon"], True),  # flag given to yna, before the subcommand
        (["ynamazon"], False),
    ],
)
def test_cli_sets_one_key(args, expected):
    assert _one_key_seen_by_process(args) is expected


@pytest.mark.parametrize(("args", "expected"), [(["--one-key"], True), ([], False)])
def test_plain_yna_sets_one_key(args, expected):
    """`yna` with no subcommand re-runs ynamazon from the command line, which must keep the flag."""
    assert _one_key_seen_by_process(args) is expected
