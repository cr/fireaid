"""
On a terminal, fireaid colours usage text and the help it prints.
"""

import re

import pytest

from test_terminal import run_on_terminal

ANSI = re.compile(r"\x1b\[[0-9;]*m")
HEADING = "\x1b[1;94m{}\x1b[0m"
NAME = "\x1b[92m{}\x1b[0m"
PLACEHOLDER = "\x1b[33m{}\x1b[0m"
LABEL = "\x1b[94m{}\x1b[0m"

COMMANDS = ["", "static", "--help", "help", "foo --help", "help foo", "static --help"]


@pytest.mark.parametrize("command", COMMANDS + ["nonesuch", "foo", "help nonesuch"])
def test_colour_adds_nothing_but_colour(run, command):
    plain = run(command)
    coloured = run(command, FORCE_COLOR="1")
    assert "\x1b[" not in plain.out + plain.err
    assert "\x1b[" in coloured.out + coloured.err
    assert ANSI.sub("", coloured.out) == plain.out
    assert ANSI.sub("", coloured.err) == plain.err
    assert coloured.code == plain.code


def test_usage_colours(run):
    out = run("", FORCE_COLOR="1").out
    assert out.startswith(HEADING.format("Usage:") + " cli.py <")
    assert "  " + LABEL.format("available commands:") in out
    assert NAME.format("foo") + " | " + NAME.format("grep") in out
    assert "\nFor detailed information on this command, run:\n  cli.py --help\n" in out


def test_usage_colours_on_an_error(run):
    err = run("foo", FORCE_COLOR="1").err
    assert "\n" + HEADING.format("Usage:") + " cli.py foo NAME <flags>\n" in err
    assert "  " + LABEL.format("optional flags:") in err
    assert NAME.format("--count") in err


def test_wrapped_usage_list_colours_names_only(run):
    # The methods of the string that foo returns fill several lines.
    err = run("foo x 1 nonesuch", FORCE_COLOR="1").err
    assert NAME.format("capitalize") + " | " + NAME.format("casefold") in err
    assert re.search(r"\x1b\[0m \|\n {20,}\x1b\[92m", err)
    assert "|\x1b[0m" not in err


def test_help_colours(run):
    out = run("foo --help", FORCE_COLOR="1").out
    for heading in ("NAME", "SYNOPSIS", "DESCRIPTION", "POSITIONAL ARGUMENTS", "FLAGS"):
        assert "\n" + HEADING.format(heading) + "\n" in "\n" + out
    assert "    cli.py foo " + PLACEHOLDER.format("NAME") + " <flags>\n" in out
    assert "    " + NAME.format("-c, --count") + "=" + PLACEHOLDER.format("COUNT") + "\n" in out
    assert "    cli.py foo - Foo doc.\n" in out
    # Only the headings are bold.
    assert out.count("\x1b[1;") == out.count("\x1b[1;94m") == 6
    assert "\n    " + PLACEHOLDER.format("NAME") + "\n" in out


def test_help_colours_the_names_of_commands(run):
    out = run("--help", component="INSTANCE", FORCE_COLOR="1").out
    for name in ("foo", "help", "static"):
        assert "\n     " + NAME.format(name) + "\n" in out
    assert "    " + PLACEHOLDER.format("COMMAND") + " is one of the following:" in out
    # An indented word elsewhere is not a name.
    assert NAME.format("Top") not in out


def test_native_help_is_fires(run):
    for command in ("foo -- --help", "-- --help"):
        assert run(command, FORCE_COLOR="1") == run(
            command, module="fire", help=True, FORCE_COLOR="1"
        )


def test_no_color_wins(run):
    for command in ("", "foo --help"):
        result = run(command, FORCE_COLOR="1", NO_COLOR="1")
        assert "\x1b[" not in result.out + result.err


def test_colour_is_on_by_default_on_a_terminal():
    assert HEADING.format("Usage:") in run_on_terminal("", code=2, color=True)
    assert HEADING.format("NAME") in run_on_terminal("foo --help", color=True)


@pytest.mark.parametrize("term", ["vt100", "dumb", ""])
def test_colour_is_off_on_a_terminal_without_colours(term):
    for command, code in (("", 2), ("foo --help", 0), ("nonesuch", 2)):
        output = run_on_terminal(command, code=code, color=True, term=term)
        assert "cli.py" in output
        assert "\x1b[1;94m" not in output
        assert "\x1b[92m" not in output and "\x1b[33m" not in output


@pytest.mark.parametrize("term", ["xterm", "linux", "a-terminal-of-the-future"])
def test_colour_is_on_on_a_terminal_with_colours(term):
    assert HEADING.format("Usage:") in run_on_terminal("", code=2, color=True, term=term)


def test_colour_is_off_when_output_is_redirected(run):
    result = run("foo --help")
    assert "\x1b[" not in result.out


def test_fire_is_left_as_found_by_colour(monkeypatch, capsys):
    import fire

    import fireaid

    monkeypatch.setenv("FORCE_COLOR", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    bold, underline = fire.formatting.Bold, fire.formatting.Underline

    class Program:
        def foo(self):
            """Foo doc."""

    for command in ("--help", "nonesuch --help", ""):
        with pytest.raises(fire.core.FireExit):
            fireaid.Fire(Program, command=command)
        assert fire.formatting.Bold is bold
        assert fire.formatting.Underline is underline
    assert HEADING.format("NAME") in capsys.readouterr().out
