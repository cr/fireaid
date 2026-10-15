"""
Type hints inform the parsing of values: str means the string as typed.
"""

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
