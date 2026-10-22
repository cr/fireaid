"""
The program under test. Not a test module itself.

FIREAID_TEST_MODULE selects fire or fireaid, FIREAID_TEST_COMPONENT the
component passed to Fire(), NONE standing for no component at all.
FIREAID_TEST_HELP gives the component fireaid's help command up front,
to see what Fire makes of it as a command like any other.
FIREAID_TEST_REMOTE makes it a remote-controlled program, whose group
static only runs locally. FIREAID_TEST_ALLOW, commas between command
paths, is then all that runs remotely.
"""

from __future__ import annotations  # annotations are strings, as in most programs

import datetime
import decimal
import enum
import fractions
import ipaddress
import os
import pathlib
import signal
import subprocess
import sys
import time
import uuid
from typing import NewType, Optional

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

    def chain(self):
        """Chain doc: returns a group, so trailing words chain onto the result."""
        return Sub()


class Group:
    def bar(self):
        """Group bar doc."""
        return "group bar"


class Device:
    """Commands that test how an allow list holds as a command runs."""

    group = Group  # a group given as a class, which Fire instantiates on the way

    def on(self):
        """Switch on, returning the device for chaining."""
        return self

    def firmware(self, path="x"):
        """Firmware doc."""
        return f"FLASHED {path}"

    @property
    def gadget(self):
        """A property whose code has an effect."""
        print("GADGET CODE RAN", file=sys.stderr)
        return "gadget"

    def sync(self):
        """Run a sub-tool of its own through Fire, as some commands do."""
        return fire.Fire(Sub, command=["bar"], name="sub")


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

    def deaf(self, seconds=30):
        """Sleep, ignoring Ctrl-C, as a command busy with what it must finish."""
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        return self.sleep(seconds)

    def resized(self, seconds=10):
        """Say started, then the terminal's size once it has changed."""
        changed = []
        signal.signal(signal.SIGWINCH, lambda *_: changed.append(True))
        print("started", flush=True)
        deadline = time.monotonic() + seconds
        while not changed and time.monotonic() < deadline:
            time.sleep(0.05)
        columns, lines = os.get_terminal_size(1)
        return f"{columns}x{lines}"

    def stubborn(self, seconds=30):
        """Sleep, ignoring SIGHUP, as a command that cleans up first would."""
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        return self.sleep(seconds)

    def spawn(self, seconds=15):
        """Leave a process running, holding stdout and stderr, as a command starting a daemon would."""
        subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])
        return "spawned"


class Configured(CLI):
    """Configured doc."""

    def __init__(self, verbose=False):
        super().__init__()
        self.verbose = verbose


class Fragile:
    """Fragile doc."""

    def __init__(self, verbose=False):
        raise RuntimeError("cannot start here")

    def go(self):
        """Go doc."""


class Undocumented:
    def __init__(self, verbose=False):
        self.verbose = verbose

    def go(self):
        """Go doc."""


class Needy:
    """Needy doc."""

    def __init__(self, path):
        self.path = path

    def go(self):
        """Go doc."""
        return self.path


class Typed:
    """Commands with type hints."""

    def __init__(self, password: str | None = None, verbose: bool = False):
        self.password = password
        self.verbose = verbose

    def who(self):
        """Who doc."""
        return f"who password={self.password!r} verbose={self.verbose!r}"

    def text(self, word: str, count: int = 1, free=None):
        """Text doc."""
        return f"text word={word!r} count={count!r} free={free!r}"

    def maybe(self, word: Optional[str] = None, other: "str | int" = 0):
        """Maybe doc."""
        return f"maybe word={word!r} other={other!r}"

    @fire.decorators.SetParseFns(word=str.upper)
    def own(self, word: str):
        """Own doc: the program's parse function wins."""
        return f"own word={word!r}"

    @fire.decorators.SetParseFn(str.upper)
    def default(self, word: str, *rest):
        """Default doc: the program's default parse function wins."""
        return f"default word={word!r} rest={rest!r}"


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


Port = NewType("Port", int)


class Kinds:
    """Commands with hints of many kinds."""

    def __init__(self, level: Level = Level.LOW):
        self.level = level

    def num(self, count: int = 0, ratio: float = 0.0, z: complex = 0j):
        """Num doc."""
        return f"num level={self.level!r} count={count!r} ratio={ratio!r} z={z!r}"

    def exact(self, money: decimal.Decimal = decimal.Decimal(0), part: fractions.Fraction = fractions.Fraction(0)):
        """Exact doc."""
        return f"exact money={money!r} part={part!r}"

    def ident(self, path: pathlib.Path = pathlib.Path("."), uid: Optional[uuid.UUID] = None,
              host: Optional[ipaddress.IPv4Address] = None, port: Port = Port(0)):
        """Ident doc."""
        return f"ident path={path!r} uid={uid!r} host={host!r} port={port!r}"

    def when(self, day: datetime.date = datetime.date(2000, 1, 1), at: Optional[datetime.datetime] = None,
             tick: datetime.time = datetime.time(0), wait: datetime.timedelta = datetime.timedelta(0)):
        """When doc."""
        return f"when day={day!r} at={at!r} tick={tick!r} wait={wait!r}"

    def choice(self, color: Color = Color.RED, flag: bool = False, bare=None):
        """Choice doc."""
        return f"choice color={color!r} flag={flag!r} bare={bare!r}"


def typed(word: str, count: int = 1):
    """Typed doc."""
    return f"typed word={word!r} count={count!r}"


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
            def setup(debug):
                print(f"SETUP debug={debug}", file=sys.stderr, flush=True)
            allow = os.environ.get("FIREAID_TEST_ALLOW")
            allow = tuple(allow.split(",")) if allow is not None else None
            fire.Fire(component, remote=fire.Remote(deny=("static",), allow=allow, setup=setup))
        else:
            fire.Fire(component)
