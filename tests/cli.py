"""
The program under test. Not a test module itself.

FIREAID_TEST_MODULE selects fire or fireaid, FIREAID_TEST_COMPONENT the
component passed to Fire(), NONE standing for no component at all.
FIREAID_TEST_HELP gives the component fireaid's help command up front,
to see what Fire makes of it as a command like any other.
FIREAID_TEST_REMOTE makes it a remote-controlled program, whose group
static only runs locally.
"""

import os
import sys
import time

fire = __import__(os.environ.get("FIREAID_TEST_MODULE", "fireaid"))


class Sub:
    def bar(self, x=1):
        """Bar doc."""
        return f"bar x={x}"

    def hosty(self, host="localhost"):
        """Hosty doc."""
        return f"hosty host={host}"


class CLI:
    """Top doc."""

    static = Sub()

    def __init__(self):
        self.dynamic = Sub()

    def foo(self, name, count=1):
        """Foo doc."""
        return f"foo name={name!r} count={count}"

    def grep(self, pattern, h=False):
        """Grep doc."""
        return f"grep pattern={pattern!r} h={h}"

    def dash_name(self, height=1):
        """Dash doc."""
        return f"dash_name height={height}"

    def echo(self, *words):
        """Echo doc."""
        return " ".join(map(str, words))

    def kw(self, **opts):
        """Kw doc."""
        return f"kw {sorted(opts.items())}"

    def effect(self, word="result"):
        """Effect doc."""
        print("EFFECT")
        return word


class WithHelp(CLI):
    def help(self, topic="general"):
        """The program's own help command."""
        return f"own help topic={topic}"


def fn(word, help=False):
    """Fn doc."""
    return f"fn word={word!r} help={help}"


def make():
    """Make doc."""
    return Sub()


class Probe(CLI):
    """Commands that show what a command line runs in."""

    def tty(self):
        """Which of stdin, stdout and stderr are terminals, and the size."""
        flags = "".join("1" if os.isatty(fd) else "0" for fd in (0, 1, 2))
        try:
            columns, lines = os.get_terminal_size(next(fd for fd in (1, 2, 0) if os.isatty(fd)))
            size = f"{columns}x{lines}"
        except (StopIteration, OSError):
            size = "-"
        return f"{flags} {size}"

    def env(self, *names):
        """Environment variables."""
        return " ".join(f"{name}={os.environ.get(name, '-')}" for name in names)

    def cat(self):
        """Copy stdin to stdout."""
        sys.stdout.flush()
        sys.stdout.buffer.write(sys.stdin.buffer.read())

    def fail(self, code=3):
        """Exit with a code."""
        sys.exit(code)

    def color(self):
        """Red on stderr where it is a terminal."""
        print("\x1b[31mred\x1b[0m" if os.isatty(2) else "plain", file=sys.stderr)
        return "result"

    def interleave(self, lines=500):
        """Alternate lines on stdout and stderr."""
        for i in range(lines):
            print(i, file=(sys.stdout, sys.stderr)[i % 2], flush=True)

    def sleep(self, seconds=30):
        """Sleep, and say so on stderr when interrupted."""
        print("started", flush=True)
        try:
            time.sleep(seconds)
        except KeyboardInterrupt:
            # Whether Python prints a traceback for an uncaught one varies.
            print("interrupted", file=sys.stderr, flush=True)
            sys.exit(130)
        return "slept"


class Configured(CLI):
    """Configured doc."""

    def __init__(self, verbose=False):
        super().__init__()
        self.verbose = verbose


class Needy:
    """Needy doc."""

    def __init__(self, path):
        self.path = path

    def go(self):
        """Go doc."""
        return self.path


DICT = {"foo": CLI().foo, "help": lambda: "own help"}

INSTANCE = CLI()


if __name__ == "__main__":
    which = os.environ.get("FIREAID_TEST_COMPONENT", "CLI")
    if which == "NONE":
        fire.Fire()
    else:
        component = globals()[which]
        if os.environ.get("FIREAID_TEST_HELP"):
            import fireaid

            component, _ = fireaid._add_help(component)
        if os.environ.get("FIREAID_TEST_REMOTE"):
            fire.Fire(component, remote=fire.Remote(deny=("static",)))
        else:
            fire.Fire(component)
