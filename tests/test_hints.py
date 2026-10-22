"""
Type hints inform the parsing of values: str means the string as typed,
other hints the type's own reading of the text.
"""

import datetime
import decimal
import enum
import fractions
import ipaddress
import pathlib
import uuid
from typing import NewType, Optional, Union

import fire
import pytest

import fireaid


@pytest.mark.parametrize(
    "command, out",
    [
        # Fire would make these an int, a float and a list.
        ("text 5", "text word='5' count=1 free=None\n"),
        ("text 1e3", "text word='1e3' count=1 free=None\n"),
        ("text --word=[1,2]", "text word='[1,2]' count=1 free=None\n"),
        # Other hints, and no hint, are left to Fire.
        ("text x --count=2", "text word='x' count=2 free=None\n"),
        ("text x --free=2", "text word='x' count=1 free=2\n"),
        ("text x --free=[1]", "text word='x' count=1 free=[1]\n"),
        # Optional and unions with None, as objects and as strings.
        ("maybe 5", "maybe word='5' other=0\n"),
        ("maybe --other=5", "maybe word=None other=5\n"),
        # The constructor's flags, from anywhere on the line.
        ("--password 1234 who", "who password='1234' verbose=False\n"),
        ("who --password=1e3 --verbose", "who password='1e3' verbose=True\n"),
        ("who", "who password=None verbose=False\n"),
        # The program's own parse functions win.
        ("own 5", "own word='5'\n"),
        ("own abc", "own word='ABC'\n"),
        ("default abc 1 2", "default word='ABC' rest=('1', '2')\n"),
    ],
)
def test_str_hint_keeps_the_string(run, command, out):
    result = run(command, component="Typed")
    assert (result.code, result.out, result.err) == (0, out, "")


def test_single_function(run):
    assert run("5 --count=2", component="typed").out == "typed word='5' count=2\n"


def test_fire_itself_is_unchanged(run):
    assert run("text 5", component="Typed", module="fire").out == "text word=5 count=1 free=None\n"
    assert run("5", component="typed", module="fire").out == "typed word=5 count=1\n"


def test_help_is_unchanged(run):
    ours = run("text --help", component="Typed")
    assert (ours.code, ours.err) == (0, "")
    assert ours.out == run("text -- --help", component="Typed", module="fire").err
    assert "--count=COUNT" in ours.out


# -- In process


def typed(word: str, count: int = 1):
    return word, count


class Program:
    def __init__(self, name: str = "x"):
        self.name = name

    def go(self, word: str):
        return self.name, word

    def fail(self, word: str):
        raise RuntimeError(word)


def test_in_process():
    assert fireaid.Fire(typed, command=["5"]) == ("5", 1)
    # Fire's quotes are not interpreted either.
    assert fireaid.Fire(typed, command=['"5"']) == ('"5"', 1)
    assert fire.Fire(typed, command=['"5"']) == ("5", 1)
    assert fireaid.Fire(Program, command=["--name", "7", "go", "8"]) == ("7", "8")


def test_fire_is_restored_after_the_call():
    original = fire.decorators.GetMetadata
    assert fireaid.Fire(typed, command=["5"]) == ("5", 1)
    assert fire.decorators.GetMetadata is original
    with pytest.raises(RuntimeError):
        fireaid.Fire(Program, command=["fail", "5"])
    assert fire.decorators.GetMetadata is original
    # Fire parses as it always did.
    assert fire.Fire(typed, command=["5"]) == (5, 1)


def test_metadata_is_not_written_to_the_program():
    assert fireaid.Fire(typed, command=["5"]) == ("5", 1)
    assert not hasattr(typed, fire.decorators.FIRE_METADATA)


@pytest.mark.parametrize(
    "hint, expected",
    [
        (str, True),
        ("str", True),
        ("str | None", True),
        ("None | str", True),
        ("Optional[str]", True),
        ("typing.Optional[str]", True),
        (int, False),
        ("int", False),
        ("str | int", False),
        ("list[str]", False),
        ("Optional[int]", False),
        ("", False),
        (None, False),
    ],
)
def test_hints_that_mean_str(hint, expected):
    assert fireaid._is_str_hint(hint) is expected


def test_object_hints_that_mean_str():
    from typing import Optional, Union

    assert fireaid._is_str_hint(Optional[str]) is True
    assert fireaid._is_str_hint(Union[str, None]) is True
    assert fireaid._is_str_hint(Union[str, int]) is False
    assert fireaid._is_str_hint(Optional[int]) is False
    assert fireaid._is_str_hint(list) is False


# -- Other hints, in process


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"
    BLUE = "blue"
    AZURE = "blue"  # an alias


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Port(int):
    pass


UserId = NewType("UserId", str)
Count = NewType("Count", int)

D = datetime.date
DT = datetime.datetime
T = datetime.time
TD = datetime.timedelta


@pytest.mark.parametrize(
    "hint, text, expected",
    [
        (int, "5", 5),
        (int, "-5", -5),
        (int, "+5", 5),
        (int, " 5 ", 5),
        (int, "1_000", 1000),
        (int, "0x10", 16),
        (int, "0b101", 5),
        (float, "5", 5.0),
        (float, "1e3", 1000.0),
        (float, "inf", float("inf")),
        (complex, "1+2j", 1 + 2j),
        (decimal.Decimal, "1.10", decimal.Decimal("1.10")),
        (fractions.Fraction, "1/3", fractions.Fraction(1, 3)),
        (fractions.Fraction, "0.5", fractions.Fraction(1, 2)),
        (pathlib.Path, "123", pathlib.Path("123")),
        (pathlib.PurePosixPath, "a/b", pathlib.PurePosixPath("a/b")),
        (uuid.UUID, "12345678123456781234567812345678", uuid.UUID("12345678-1234-5678-1234-567812345678")),
        (ipaddress.IPv4Address, "1.2.3.4", ipaddress.IPv4Address("1.2.3.4")),
        (ipaddress.IPv6Address, "::1", ipaddress.IPv6Address("::1")),
        (ipaddress.IPv4Network, "10.0.0.0/8", ipaddress.IPv4Network("10.0.0.0/8")),
        (ipaddress.IPv4Interface, "10.0.0.1/8", ipaddress.IPv4Interface("10.0.0.1/8")),
        # Subclasses and NewTypes go through the type they are made of.
        (Port, "80", Port(80)),
        (UserId, "42", "42"),
        (Count, "42", 42),
        # Dates and times, as fromisoformat reads them.
        (D, "2026-10-15", D(2026, 10, 15)),
        (DT, "2026-10-15", DT(2026, 10, 15)),
        (DT, "2026-10-15T12:30", DT(2026, 10, 15, 12, 30)),
        (DT, "2026-10-15 12:30:15+02:00", DT(2026, 10, 15, 12, 30, 15, tzinfo=datetime.timezone(datetime.timedelta(hours=2)))),
        (T, "12:30", T(12, 30)),
        (T, "12:30:15.500000", T(12, 30, 15, 500000)),
        # Durations: seconds, units, or the clock form str() prints.
        (TD, "90", TD(seconds=90)),
        (TD, "1.5", TD(seconds=1.5)),
        (TD, "-30", TD(seconds=-30)),
        (TD, "1h30m", TD(hours=1, minutes=30)),
        (TD, "2d", TD(days=2)),
        (TD, "1.5h", TD(minutes=90)),
        (TD, "250ms", TD(milliseconds=250)),
        (TD, "1d2h3m4s5ms", TD(days=1, hours=2, minutes=3, seconds=4, milliseconds=5)),
        (TD, "-1h", TD(hours=-1)),
        (TD, "1:30:00", TD(hours=1, minutes=30)),
        (TD, "0:00:01.500000", TD(seconds=1.5)),
        (TD, "1:30", TD(hours=1, minutes=30)),
        (TD, "-1:30:00", TD(hours=-1, minutes=-30)),
        (TD, str(TD(hours=25, seconds=1)), TD(hours=25, seconds=1)),
        # Enums: by name, case-insensitively, then by value.
        (Color, "red", Color.RED),
        (Color, "RED", Color.RED),
        (Color, "Green", Color.GREEN),
        (Color, "blue", Color.BLUE),
        (Color, "azure", Color.BLUE),
        (Level, "low", Level.LOW),
        (Level, "2", Level.HIGH),
        # Booleans: words, not bool().
        (bool, "True", True),
        (bool, "False", False),
        (bool, "yes", True),
        (bool, "NO", False),
        (bool, "on", True),
        (bool, "off", False),
        (bool, "1", True),
        (bool, "0", False),
        # Optional: the text None is None, anything else is the type's.
        (Optional[int], "None", None),
        (Optional[int], "5", 5),
        (Optional[Color], "None", None),
        (Optional[Color], "red", Color.RED),
        (Union[None, D], "2026-10-15", D(2026, 10, 15)),
        (Optional[UserId], "None", None),
        (Optional[str], "None", None),
        (str, "None", "None"),
    ],
)
def test_hinted_value(hint, text, expected):
    def fn(value: hint = None):
        return value

    fn.__annotations__ = {"value": hint}
    assert fireaid.Fire(fn, command=["--value", text]) == expected


@pytest.mark.parametrize(
    "hint, text, form",
    [
        (int, "1e3", "an integer"),
        (int, "5.0", "an integer"),
        (int, "abc", "an integer"),
        (int, "007", "an integer"),
        (float, "abc", "a number"),
        (complex, "abc", "a complex number"),
        (decimal.Decimal, "abc", "a decimal number"),
        (fractions.Fraction, "1/0", "a fraction"),
        (uuid.UUID, "abc", "a UUID"),
        (ipaddress.IPv4Address, "abc", "an IP address"),
        (ipaddress.IPv4Network, "10.0.0.1/8", "an IP network"),
        (D, "15.10.2026", "a date, YYYY-MM-DD"),
        (DT, "yesterday", "a date and time in ISO 8601"),
        (T, "noon", "a time, HH:MM[:SS]"),
        (TD, "1m30", "a duration: seconds, 1h30m or H:MM:SS"),
        (TD, "1:2:3:4", "a duration: seconds, 1h30m or H:MM:SS"),
        (TD, "inf", "a duration: seconds, 1h30m or H:MM:SS"),
        (TD, "nan", "a duration: seconds, 1h30m or H:MM:SS"),
        (Color, "pink", "one of red, green, blue, azure"),
        (Level, "3", "one of low, high"),
        (bool, "maybe", "true or false"),
        (Optional[int], "none", "an integer"),
        (Port, "x", "an integer"),
    ],
)
def test_hinted_value_rejected(hint, text, form, capsys):
    def fn(value: hint = None):
        return value

    fn.__annotations__ = {"value": hint}
    with pytest.raises(fire.core.FireExit) as e:
        fireaid.Fire(fn, command=["--value", text])
    assert e.value.code == 2
    # Whether the label is coloured depends on the terminal.
    err = fireaid._ANSI.sub("", capsys.readouterr().err)
    assert err.startswith(f"ERROR: value: {text!r} is not {form}\nUsage:")


@pytest.mark.parametrize(
    "hint, text, expected",
    [
        # Hints fireaid does not read are Fire's: unions, containers, Any, classes of its own.
        (Union[int, str], "5", 5),
        ("list[int]", "[1,2]", [1, 2]),
        (list, "[1,2]", [1, 2]),
        (object, "5", 5),
        (bytes, "5", 5),
        (bytes, "abc", "abc"),
        ("Nonesuch", "5", 5),
        ("str | int", "5", 5),
    ],
)
def test_other_hints_are_fires(hint, text, expected):
    def fn(value=None):
        return value

    fn.__annotations__ = {"value": hint}
    assert fireaid.Fire(fn, command=["--value", text]) == expected


def test_bare_parameters_are_fires():
    def fn(value=None, *rest):
        return value, rest

    assert fireaid.Fire(fn, command=["5", "1e3", "[1]"]) == (5, (1000.0, [1]))


def test_bool_flags():
    def fn(flag: bool = False):
        return flag

    assert fireaid.Fire(fn, command=["--flag"]) is True
    assert fireaid.Fire(fn, command=["--noflag"]) is False
    assert fireaid.Fire(fn, command=["--flag=yes"]) is True
    assert fireaid.Fire(fn, command=[]) is False


def test_subclass_of_int_with_its_own_constructor():
    class Port(int):
        def __new__(cls, value):
            return super().__new__(cls, value)

    def fn(port: Port):
        return port

    port = fireaid.Fire(fn, command=["0x50"])
    assert type(port) is Port and port == 80


def test_generic_hints_are_not_classes():
    # On Python 3.9 and 3.10 inspect.isclass(list[int]) is True, and issubclass raises.
    for hint in (list[int], dict[str, int], Optional[list[int]], tuple[int, ...]):
        assert fireaid._parser_for(hint) is None


def test_subclass_of_datetime():
    class Stamp(datetime.datetime):
        pass

    def fn(at: Stamp):
        return at

    at = fireaid.Fire(fn, command=["2026-10-15T01:02"])
    assert type(at) is Stamp and at == Stamp(2026, 10, 15, 1, 2)


# -- Through the test program, whose annotations are strings


@pytest.mark.parametrize(
    "command, out",
    [
        ("num --count 0x10 --ratio 5 --z 1+2j", "num level=<Level.LOW: 1> count=16 ratio=5.0 z=(1+2j)\n"),
        ("--level high num", "num level=<Level.HIGH: 2> count=0 ratio=0.0 z=0j\n"),
        ("exact --money 1.10 --part 1/3", "exact money=Decimal('1.10') part=Fraction(1, 3)\n"),
        ("ident --path 123 --uid 12345678123456781234567812345678 --host 10.0.0.1 --port 80",
         "ident path=PosixPath('123') uid=UUID('12345678-1234-5678-1234-567812345678') "
         "host=IPv4Address('10.0.0.1') port=80\n"),
        ("ident --uid None", "ident path=PosixPath('.') uid=None host=None port=0\n"),
        ("when --day 2026-10-15 --at 2026-10-15T12:30 --tick 12:30 --wait 1h30m",
         "when day=datetime.date(2026, 10, 15) at=datetime.datetime(2026, 10, 15, 12, 30) "
         "tick=datetime.time(12, 30) wait=datetime.timedelta(seconds=5400)\n"),
        ("choice --color green --flag --bare 1e3", "choice color=<Color.GREEN: 'green'> flag=True bare=1000.0\n"),
        ("choice --noflag", "choice color=<Color.RED: 'red'> flag=False bare=None\n"),
    ],
)
def test_program_with_string_annotations(run, command, out):
    result = run(command, component="Kinds")
    assert (result.code, result.out, result.err) == (0, out, "")


def test_program_error_shows_usage(run):
    result = run("num --count 1e3", component="Kinds")
    assert (result.code, result.out) == (2, "")
    assert result.err.startswith("ERROR: count: '1e3' is not an integer\nUsage: cli.py num <flags>")
    result = run("choice --color pink", component="Kinds")
    assert result.err.startswith("ERROR: color: 'pink' is not one of red, green\n")


def test_program_help_is_unchanged(run):
    ours = run("when --help", component="Kinds")
    assert (ours.code, ours.err) == (0, "")
    assert ours.out == run("when -- --help", component="Kinds", module="fire").err
