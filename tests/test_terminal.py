"""
On a terminal, Fire pages its help. fireaid prints it like other tools.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

if sys.platform == "win32":
    pytest.skip("terminals here are Unix pseudo-terminals", allow_module_level=True)

import pty

TESTS = Path(__file__).parent

# Fire runs $PAGER in a shell. This one marks every line it is given.
PAGER = "sed s/^/PAGED:/"


def run_on_terminal(
    command, module="fireaid", code=0, component="CLI", color=False, term="xterm-256color"
):
    """Run the test program with a terminal as stdin, stdout and stderr."""
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            [sys.executable, "cli.py", *command.split()],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env=dict(
                {k: v for k, v in os.environ.items() if k != "FORCE_COLOR"},
                FIREAID_TEST_MODULE=module,
                FIREAID_TEST_COMPONENT=component,
                PAGER=PAGER,
                NO_COLOR="" if color else "1",
                TERM=term,
            ),
            cwd=TESTS,
        )
    finally:
        os.close(slave)
    output = b""
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
    finally:
        os.close(master)
    assert process.wait() == code
    return output.decode().replace("\r\n", "\n")


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
