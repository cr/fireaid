"""
Running the test program, tests/cli.py. Not a test module itself.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

try:
    import fcntl
    import pty
    import struct
    import termios
except ImportError:  # Windows
    pty = None

TESTS = Path(__file__).parent

unix = pytest.mark.skipif(pty is None, reason="needs Unix terminals and signals")

# Fire runs $PAGER in a shell. This one marks every line it is given.
MARKING_PAGER = "sed s/^/PAGED:/"


def environment(module="fireaid", component="CLI", **variables):
    """
    The test program's environment: fire or fireaid, the component, no
    colour settings, the pager cat and 80 columns, unless variables say
    otherwise.
    """
    env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "FORCE_COLOR")}
    env.update(FIREAID_TEST_MODULE=module, FIREAID_TEST_COMPONENT=component, PAGER="cat", COLUMNS="80")
    env.update(variables)
    return env


def on_terminal(argv, env, stdout_tty=True, keys=None, wait_for=b""):
    """
    Run the test program with a terminal of 80x24 as stdin and stderr,
    and as stdout too unless stdout_tty is false: then stdout is a pipe.
    Writes keys to the terminal once wait_for has appeared there.
    Returns the exit code, the terminal's output and stdout's.
    """
    master, slave = pty.openpty()
    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    try:
        process = subprocess.Popen(
            [sys.executable, "cli.py", *argv],
            stdin=slave,
            stdout=slave if stdout_tty else subprocess.PIPE,
            stderr=slave,
            env=env,
            cwd=TESTS,
            # Its own terminal, as from a shell: Ctrl-C typed there reaches it.
            start_new_session=True,
            preexec_fn=lambda: fcntl.ioctl(0, termios.TIOCSCTTY, 0),
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
            if keys is not None and wait_for in output:
                os.write(master, keys)
                keys = None
    finally:
        os.close(master)
    stdout = process.stdout.read().decode() if process.stdout else ""
    return process.wait(30), output.decode().replace("\r\n", "\n"), stdout


def run_on_terminal(command, module="fireaid", code=0, component="CLI", color=False, term="xterm-256color"):
    """
    Run the test program on a terminal, with a pager that marks what it
    is given. Returns what the terminal shows.
    """
    env = environment(module, component, PAGER=MARKING_PAGER, NO_COLOR="" if color else "1", TERM=term)
    returncode, output, _ = on_terminal(command.split(), env)
    assert returncode == code
    return output
