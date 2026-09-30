"""
What fireaid leaves alone: everything a program could mean otherwise.
"""

import pytest


@pytest.mark.parametrize(
    "component, command, out",
    [
        # Ordinary commands.
        ("CLI", "foo x", "foo name='x' count=1\n"),
        ("CLI", "foo x --count=2", "foo name='x' count=2\n"),
        ("CLI", "static bar --x 3", "bar x=3\n"),
        ("CLI", "dynamic bar", "bar x=1\n"),
        ("NONE", "fn x", "fn word='x' help=False\n"),
        ("NONE", "CLI foo x", "foo name='x' count=1\n"),
        # help is only help as the first word.
        ("CLI", "foo help", "foo name='help' count=1\n"),
        ("CLI", "echo say help", "say help\n"),
        # The program's own help command.
        ("WithHelp", "help", "own help topic=general\n"),
        ("WithHelp", "help foo", "own help topic=foo\n"),
        ("DICT", "help", "own help\n"),
        # A single function.
        ("fn", "help", "fn word='help' help=False\n"),
        # A command with a help parameter.
        ("fn", "x --help", "fn word='x' help=True\n"),
        # Commands with a parameter starting with h.
        ("CLI", "grep pat -h", "grep pattern='pat' h=True\n"),
        ("CLI", "static hosty -h", "hosty host=True\n"),
        ("CLI", "dynamic hosty -h", "hosty host=True\n"),
        ("CLI", "dash-name -h", "dash_name height=True\n"),
        # Values.
        ("CLI", "foo --name=--help", "foo name='--help' count=1\n"),
        ("CLI", "foo --name=-h", "foo name='-h' count=1\n"),
    ],
)
def test_command_runs_as_with_fire(run, component, command, out):
    result = run(command, component=component)
    assert result.code == 0
    assert result.err == ""
    assert result.out == out
    assert result == run(command, module="fire", component=component)


@pytest.mark.parametrize(
    "component, command",
    [
        # Native syntax.
        ("CLI", "-- --help"),
        ("CLI", "foo -- --help"),
        ("CLI", "foo -- -h"),
        ("CLI", "help -- --help"),
        ("CLI", "foo x -- --trace"),
        # Help flags that are not the last word.
        ("CLI", "echo a --help b"),
        ("CLI", "foo -h x"),
        # Help flags the command takes itself, with an argument missing.
        ("CLI", "grep -h"),
        ("fn", "--help"),
        # -h for a command that cannot be determined.
        ("CLI", "dynamic bar -h"),
        # Errors.
        ("CLI", "nonesuch"),
        ("CLI", "foo"),
    ],
)
def test_output_is_fires(run, component, command):
    # Fire's own output lists the commands, the help command among them.
    result = run(command, component=component)
    assert result == run(command, module="fire", component=component, help=True)


def test_native_help_is_on_stderr(run):
    result = run("foo -- --help")
    assert result.code == 0
    assert result.out == ""
    assert "cli.py foo - Foo doc." in result.err
