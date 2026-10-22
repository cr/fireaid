import subprocess
import sys

import fire
import pytest

from support import TESTS, environment


def pytest_report_header(config):
    return f"fire: {fire.__version__}"


class Result:
    def __init__(self, completed):
        self.code = completed.returncode
        self.out = completed.stdout
        self.err = completed.stderr

    def __eq__(self, other):
        return (self.code, self.out, self.err) == (other.code, other.out, other.err)

    def __repr__(self):
        return f"Result(code={self.code!r}, out={self.out!r}, err={self.err!r})"


def _run(argv, module="fireaid", component="CLI", help=False, **variables):
    env = environment(module, component, FIREAID_TEST_HELP="1" if help else "", **variables)
    return Result(
        subprocess.run(
            [sys.executable, *argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            cwd=TESTS,
        )
    )


@pytest.fixture
def run():
    """
    Run the test program on a command line, with fireaid or with fire.

    With help, Fire is given a component that has fireaid's help command.
    Further keyword arguments are environment variables.
    """

    def run(command, module="fireaid", component="CLI", help=False, **variables):
        return _run(["cli.py", *command.split()], module, component, help, **variables)

    return run


@pytest.fixture
def run_module():
    """Run python -m fireaid, or python -m fire, on a command line."""

    def run_module(command, module="fireaid"):
        return _run(["-m", module, *command.split()])

    return run_module
