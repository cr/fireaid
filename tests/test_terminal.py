"""
On a terminal, Fire pages its help. fireaid prints it like other tools.
"""

from support import run_on_terminal, unix

pytestmark = unix


def test_fire_pages_help():
    # The premise.
    for module in ("fire", "fireaid"):
        output = run_on_terminal("foo -- --help", module)
        assert "PAGED:    cli.py foo - Foo doc.\n" in output


def test_help_is_not_paged():
    for command in ("foo --help", "foo -h", "help foo"):
        output = run_on_terminal(command)
        assert "\n    cli.py foo - Foo doc.\n" in output
        assert "PAGED" not in output


def test_fire_pages_a_group():
    # The premise. A command line naming a group gets the group's help.
    for command in ("", "static"):
        assert "PAGED:" in run_on_terminal(command, "fire")


def test_group_is_not_paged():
    for command in ("", "static"):
        output = run_on_terminal(command, code=2)
        assert output.startswith("Usage: cli.py")
        assert "PAGED" not in output


def test_returned_object_is_not_paged():
    assert "PAGED:" in run_on_terminal("", "fire", component="make")
    output = run_on_terminal("", component="make")
    assert "\n    cli.py\n" in output
    assert "PAGED" not in output
