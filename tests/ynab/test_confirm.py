"""Tests for y/n prompts and --one-key."""

from unittest.mock import Mock, patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from ynamazon import confirm as confirm_module
from ynamazon.confirm import confirm, one_key_enabled, set_one_key


@pytest.fixture
def one_key():
    """Turn single-key answers on with a terminal for stdin, and restore afterwards."""
    set_one_key(True)
    with patch("ynamazon.confirm.sys.stdin") as stdin:
        stdin.isatty.return_value = True
        yield
    set_one_key(False)


def _console() -> Console:
    return Console(file=Mock(), force_terminal=False)


@pytest.mark.parametrize(("keys", "expected"), [(["y"], True), (["Y"], True), (["n"], False), (["N"], False)])
def test_one_key_answers_without_enter(one_key, keys, expected):
    with patch("ynamazon.confirm.click.getchar", side_effect=keys) as getchar, \
         patch("ynamazon.confirm.Confirm.ask") as ask:
        assert confirm("Update?", console=_console()) is expected

    assert getchar.call_count == 1
    ask.assert_not_called()


def test_one_key_ignores_other_keys(one_key):
    with patch("ynamazon.confirm.click.getchar", side_effect=["x", "\r", " ", "n"]) as getchar:
        assert confirm("Update?", console=_console()) is False
    assert getchar.call_count == 4


def test_one_key_ctrl_c_still_interrupts(one_key):
    with patch("ynamazon.confirm.click.getchar", side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
        confirm("Update?", console=_console())


def test_one_key_echoes_answer(one_key):
    console = Console(record=True, force_terminal=False, width=80)
    with patch("ynamazon.confirm.click.getchar", return_value="y"):
        confirm("Update?", console=console)
    assert console.export_text() == "Update? [y/n]: y\n"


def test_default_uses_normal_prompt():
    set_one_key(False)
    with patch("ynamazon.confirm.Confirm.ask", return_value=True) as ask, \
         patch("ynamazon.confirm.click.getchar") as getchar:
        assert confirm("Update?") is True
    ask.assert_called_once()
    getchar.assert_not_called()


def test_one_key_falls_back_to_normal_prompt_when_input_is_piped():
    set_one_key(True)
    try:
        with patch("ynamazon.confirm.sys.stdin") as stdin, \
             patch("ynamazon.confirm.Confirm.ask", return_value=False) as ask, \
             patch("ynamazon.confirm.click.getchar") as getchar:
            stdin.isatty.return_value = False
            assert confirm("Update?") is False
        ask.assert_called_once()
        getchar.assert_not_called()
    finally:
        set_one_key(False)


@pytest.mark.parametrize(("args", "expected"), [(["--one-key"], True), ([], False)])
def test_cli_sets_one_key(args, expected):
    from ynamazon.cli.cli import cli

    seen = {}
    with patch("ynamazon.cli.cli.process_transactions", side_effect=lambda **_: seen.update(on=one_key_enabled())):
        result = CliRunner().invoke(cli, ["ynamazon", *args])
    confirm_module.set_one_key(False)

    assert result.exit_code == 0, result.output
    assert seen["on"] is expected
