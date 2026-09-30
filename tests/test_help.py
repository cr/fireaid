"""
What fireaid adds: Git-style help, and clean help on stdout.
"""

import pytest

BANNER = "INFO: Showing help"


def native_help(run, path, component="CLI"):
    """The help text Fire prints for its native syntax."""
    result = run(f"{path} -- --help", module="fire", component=component)
    assert result.code == 0 and result.err.startswith("NAME")
    return result.err


@pytest.mark.parametrize(
    "command, path",
    [
        ("--help", ""),
        ("-h", ""),
        ("help", ""),
        ("foo --help", "foo"),
        ("foo -h", "foo"),
        ("help foo", "foo"),
        ("static bar --help", "static bar"),
        ("static bar -h", "static bar"),
        ("help static bar", "static bar"),
        ("help dynamic bar", "dynamic bar"),
        ("dash-name --help", "dash-name"),
        ("help dash-name", "dash-name"),
    ],
)
def test_help_is_fires_help_on_stdout(run, command, path):
    result = run(command)
    assert result.code == 0
    assert result.err == ""
    assert result.out == native_help(run, path)


def test_help_names_the_command(run):
    assert "cli.py foo - Foo doc." in run("foo --help").out
    assert "cli.py static bar - Bar doc." in run("help static bar").out


def test_help_for_kwargs_function(run):
    assert run("kw --help") == run("help kw")
    assert "cli.py kw - Kw doc." in run("kw --help").out
    # Fire passes the flag to the function instead.
    assert run("kw --help", module="fire").out == "kw [('help', True)]\n"


def test_help_for_unresolved_command(run):
    # The member is created in __init__. --help is still clean.
    result = run("dynamic bar --help")
    assert result.code == 0
    assert result.err == ""
    assert result.out == native_help(run, "dynamic bar")


def test_help_is_for_the_result_of_the_command_line(run):
    result = run("effect x --help")
    assert result.code == 0
    assert result.err == ""
    assert result.out.startswith("EFFECT\n")
    assert 'The string "x"' in result.out


def test_help_for_a_command_does_not_run_it(run):
    for command in ("effect --help", "effect -h", "help effect"):
        result = run(command)
        assert result.code == 0
        assert "EFFECT" not in result.out
        assert "cli.py effect - Effect doc." in result.out


def test_help_without_a_component(run):
    for command in ("--help", "help"):
        result = run(command, component="NONE")
        assert result.code == 0
        assert result.err == ""
        assert "WithHelp" in result.out
        assert "_normalize_help" not in result.out


def test_help_below_a_component_with_own_help(run):
    result = run("foo --help", component="WithHelp")
    assert result.code == 0
    assert result.out == native_help(run, "foo", component="WithHelp")


@pytest.mark.parametrize("command", ["nonesuch --help", "help nonesuch"])
def test_help_for_an_unknown_command_is_an_error(run, command):
    result = run(command)
    assert result.code == 2
    assert result.out == ""
    assert result == run("nonesuch -- --help", module="fire", help=True)
    assert result.err.startswith("ERROR: Could not consume arg: nonesuch")


def test_fire_prints_banner_to_stderr(run):
    # The premise of all of the above.
    result = run("foo --help", module="fire")
    assert result.code == 0
    assert result.out == ""
    assert result.err.startswith(BANNER)


@pytest.mark.parametrize("command", ["", "static", "dynamic"])
def test_incomplete_command_is_an_error_with_usage(run, command):
    result = run(command)
    assert result.code == 2
    assert result.err == ""
    assert result.out.startswith(f"Usage: cli.py {command}".rstrip() + " <")
    # The usage is the one Fire prints for an error at the same place.
    fires = run(f"{command} nonesuch", module="fire", help=True)
    error, usage = fires.err.split("\n", 1)
    assert error.startswith("ERROR")
    assert result.out == usage


def test_fire_calls_an_incomplete_command_a_success(run):
    # The premise.
    result = run("static", module="fire")
    assert result.code == 0
    assert result.out.startswith("NAME")


def test_instantiated_class_is_an_incomplete_command(run):
    result = run("CLI", component="NONE")
    assert result.code == 2
    assert result.out.startswith("Usage: cli.py CLI <")


@pytest.mark.parametrize("component, command", [("make", ""), ("NONE", "make")])
def test_returned_object_is_not_an_incomplete_command(run, component, command):
    # A function ran. Fire shows help for what came back.
    result = run(command, component=component)
    assert result.code == 0
    assert result.err == ""
    assert result.out.startswith("NAME")
    assert result == run(command, module="fire", component=component)


HELP_ENTRY = "\n     help\n       Show help for the program, or for a command.\n"


def test_help_command_is_listed_in_usage(run):
    for component in ("CLI", "INSTANCE", "NONE"):
        commands = [
            line for line in run("", component=component).out.splitlines()
            if line.startswith("  available commands:")
        ]
        assert len(commands) == 1
        assert " help " in commands[0] + " "


def test_help_command_is_listed_in_help(run):
    for command in ("--help", "-h", "help"):
        result = run(command, component="INSTANCE")
        assert result.code == 0
        assert HELP_ENTRY in result.out
        fires = run("-- --help", module="fire", component="INSTANCE", help=True)
        assert result.out == fires.err


def test_help_command_is_not_listed_below_the_top(run):
    assert "help" not in run("static").out.split("For detailed")[0]
    assert HELP_ENTRY not in run("static --help").out


@pytest.mark.parametrize("command", ["help --help", "help -h", "help help"])
def test_help_for_the_help_command(run, command):
    result = run(command)
    assert result.code == 0
    assert result.err == ""
    assert "cli.py help - Show help for the program, or for a command." in result.out
    assert "cli.py help [COMMAND]..." in result.out


@pytest.mark.parametrize("command", ["help foo --help", "help foo -h"])
def test_help_with_a_help_flag(run, command):
    assert run(command) == run("help foo")


@pytest.mark.parametrize("component", ["WithHelp", "DICT", "fn"])
def test_no_help_command_where_it_is_not_ours(run, component):
    # The program's own help command, or a single function: all is Fire's.
    for command in ("nonesuch", "help -- --help"):
        result = run(command, component=component)
        assert result == run(command, module="fire", component=component)
