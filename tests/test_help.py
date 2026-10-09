"""
What fireaid adds: Git-style help, and clean help on stdout.
"""

import pytest

BANNER = "INFO: Showing help"


def native_help(run, path, component="CLI", help=False):
    """The help text Fire prints for its native syntax."""
    result = run(f"{path} -- --help", module="fire", component=component, help=help)
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
    if path:
        assert result.out == native_help(run, path)
    else:
        # CLI is a class. Fire's help for the class itself lists only its
        # constructor's flags; fireaid's is the help for an instance, the
        # help command among its commands.
        assert result.out == native_help(run, path, component="INSTANCE", help=True)


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
        lines = run("", component=component).out.splitlines()
        starts = [i for i, line in enumerate(lines) if line.startswith("  available commands:")]
        assert len(starts) == 1
        commands = lines[starts[0]]
        for line in lines[starts[0] + 1 :]:  # the list wraps onto indented lines
            if not line.startswith(" " * 20):
                break
            commands += " " + line.strip()
        assert " help " in commands + " "


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


# -- A class as the program: Fire shows its constructor's flags only.


@pytest.mark.parametrize("command", ["help", "--help", "-h"])
def test_help_for_a_class_is_help_for_its_instance(run, command):
    assert run(command, component="CLI") == run(command, component="INSTANCE")


def test_fire_does_not_instantiate_a_class_for_its_help(run):
    # The premise.
    fires = run("-- --help", module="fire", component="CLI")
    assert "     static\n" in fires.err
    assert "     dynamic\n" not in fires.err


def test_help_for_a_class_with_flags(run):
    result = run("help", component="Configured")
    assert result.code == 0 and result.err == ""
    out = result.out
    assert "\nSYNOPSIS\n    cli.py <flags> GROUP | COMMAND | VALUE\n\nDESCRIPTION\n    Configured doc.\n\nFLAGS\n" in out
    assert "\n    -v, --verbose=VERBOSE\n" in out
    assert out.index("FLAGS\n") < out.index("GROUPS\n") < out.index("COMMANDS\n") < out.index("VALUES\n")
    assert "     dynamic\n" in out and "     foo\n" in out and "     verbose\n" in out
    assert run("--help", component="Configured") == result


def test_help_for_a_class_that_needs_arguments_is_fires(run):
    result = run("help", component="Needy")
    assert result.code == 0
    assert "SYNOPSIS\n    cli.py --path=PATH\n" in result.out
    assert "ARGUMENTS\n    PATH\n" in result.out
    assert "COMMANDS" not in result.out


def test_native_help_for_a_class_is_fires(run):
    for component in ("Configured", "Needy"):
        result = run("-- --help", component=component)
        assert result == run("-- --help", module="fire", component=component, help=True)
        assert "COMMANDS" not in result.err


def test_usage_for_a_class_has_no_separator(run):
    result = run("", component="Configured")
    assert result.code == 2
    assert result.out.startswith("Usage: cli.py <group|command|value>\n")
    assert result.out.endswith("\nFor detailed information on this command, run:\n  cli.py --help\n")
    # The premise: Fire shows the separator, as the next word could be a flag.
    fires = run("", module="fire", component="Configured", help=True)
    assert "cli.py - GROUP | COMMAND | VALUE" in fires.out
    # Deeper in, there is none to take out.
    assert run("static", component="Configured").out.startswith("Usage: cli.py static <command>\n")
