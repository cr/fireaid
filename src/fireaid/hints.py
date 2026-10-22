"""
Reading values as the parameters' type hints say.

Fire reads every value on the command line as a Python literal where it
can, whatever the parameter's hint. For as long as fireaid calls Fire,
Fire gets a parse function for each parameter whose hint fireaid reads.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import fractions
import functools
import inspect
import ipaddress
import math
import pathlib
import re
import typing
import uuid
from contextlib import contextmanager
from typing import Any

from fire import core, decorators, parser

_OPTIONAL = re.compile(r"(?:typing\.)?Optional\[(.*)\]")
_WORDS = {"true": True, "yes": True, "on": True, "1": True, "false": False, "no": False, "off": False, "0": False}
_UNIT = re.compile(r"(\d+(?:\.\d+)?)(ms|d|h|m|s)")
_UNITS = re.compile(f"(?:{_UNIT.pattern})+")
_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1, "ms": 0.001}
_CLOCK = re.compile(r"(?:(-?\d+) days?, )?(-?\d+):(\d+)(?::(\d+(?:\.\d+)?))?")

# Types whose constructor reads the text, and what they read. Earlier
# entries win: an interface is an address too.
_CONSTRUCTED = (
    (str, "a string"),
    (float, "a number"),
    (complex, "a complex number"),
    (decimal.Decimal, "a decimal number"),
    (fractions.Fraction, "a fraction"),
    (pathlib.PurePath, "a path"),
    (uuid.UUID, "a UUID"),
    (ipaddress.IPv4Interface, "an IP interface"),
    (ipaddress.IPv6Interface, "an IP interface"),
    (ipaddress.IPv4Address, "an IP address"),
    (ipaddress.IPv6Address, "an IP address"),
    (ipaddress.IPv4Network, "an IP network"),
    (ipaddress.IPv6Network, "an IP network"),
)


def _unwrap(hint: Any) -> tuple[Any, bool]:
    """The type within Optional[T], T | None and a NewType, and whether None is allowed."""
    optional = False
    if isinstance(hint, str):
        # A hint that could not be evaluated: only its words are known.
        text = hint.strip()
        match = _OPTIONAL.fullmatch(text)
        if match:
            text, optional = match.group(1), True
        parts = [part.strip() for part in text.split("|")]
        if "None" in parts:
            optional = True
            parts = [part for part in parts if part != "None"]
        return (parts[0] if len(parts) == 1 else hint), optional
    args = typing.get_args(hint)
    if args:
        rest = [arg for arg in args if arg is not type(None)]
        if len(rest) == len(args) or len(rest) != 1:
            return hint, False  # a union of types, or a generic: Fire's
        hint, optional = rest[0], True
    while hasattr(hint, "__supertype__"):
        hint = hint.__supertype__
    return hint, optional


def _parse_bool(text: str) -> bool:
    try:
        return _WORDS[text.strip().lower()]
    except KeyError:
        raise ValueError(text) from None


def _parse_enum(cls: Any, text: str) -> Any:
    for name, member in cls.__members__.items():
        if name.lower() == text.lower():
            return member
    for member in cls:
        if str(member.value) == text:
            return member
    raise ValueError(text)


def _parse_timedelta(cls: Any, text: str) -> Any:
    text = text.strip()
    try:
        seconds = float(text)
    except ValueError:
        pass
    else:
        if not math.isfinite(seconds):
            raise ValueError(text)
        return cls(seconds=seconds)
    match = _CLOCK.fullmatch(text)
    if match:
        days, hours, minutes, seconds = match.groups()
        clock = cls(hours=abs(int(hours)), minutes=int(minutes), seconds=float(seconds or 0))
        if days is not None:
            return cls(days=int(days)) + clock
        return -clock if hours.startswith("-") else clock
    body = text.lstrip("+-")
    if not _UNITS.fullmatch(body):
        raise ValueError(text)
    total = sum(float(number) * _SECONDS[unit] for number, unit in _UNIT.findall(body))
    return cls(seconds=-total if text.startswith("-") else total)


def _parser_for(hint: Any) -> Any:
    """
    How to read the text for a type hint: a function, what it reads,
    and whether the text None means None. None for a hint that is left
    to Fire.
    """
    hint, optional = _unwrap(hint)
    if isinstance(hint, str):
        if hint != "str":
            return None
        hint = str
    if typing.get_origin(hint) is not None or not inspect.isclass(hint):
        return None  # a generic such as list[int] is a class on Python 3.9 and 3.10
    if hint is bool:
        parse, form = _parse_bool, "true or false"
    elif issubclass(hint, enum.Enum):
        parse = functools.partial(_parse_enum, hint)
        form = "one of " + ", ".join(name.lower() for name in hint.__members__)
    elif issubclass(hint, int):
        # The subclass may take only a value, so int reads the text first.
        parse, form = (lambda text: hint(int(text, 0))), "an integer"
    elif issubclass(hint, datetime.timedelta):
        parse, form = functools.partial(_parse_timedelta, hint), "a duration: seconds, 1h30m or H:MM:SS"
    elif issubclass(hint, datetime.datetime):
        parse, form = hint.fromisoformat, "a date and time in ISO 8601"
    elif issubclass(hint, datetime.date):
        parse, form = hint.fromisoformat, "a date, YYYY-MM-DD"
    elif issubclass(hint, datetime.time):
        parse, form = hint.fromisoformat, "a time, HH:MM[:SS]"
    else:
        form = next((form for cls, form in _CONSTRUCTED if issubclass(hint, cls)), None)
        if form is None:
            return None
        parse = hint
    return parse, form, optional


def _parse_function(name: str, parse: Any, form: str, optional: bool) -> Any:
    """A parse function for Fire, failing the way Fire reports."""

    def parse_value(text: str) -> Any:
        if optional and text == "None":
            return None
        try:
            return parse(text)
        except (ValueError, ArithmeticError, TypeError):
            raise core.FireError(f"{name}: {text!r} is not {form}") from None

    return parse_value


def _parsers(component: Any) -> tuple[dict[str, Any], Any]:
    """
    Parse functions for the hinted parameters of what Fire is about to
    call: by name, and the one for the values of ``*args`` and the flags
    of ``**kwargs``, or None.

    Fire reads those with its default parse function, which is also
    what any other parameter without a parse function of its own falls
    back to. Where there is one for them, every other parameter gets
    one, Fire's own where fireaid does not read its hint. Where a
    function has both ``*args`` and ``**kwargs``, they are read only if
    their hints are the same: with different ones, or one of them
    without, both are Fire's.
    """
    fn = component
    if inspect.isclass(component):
        fn = component.__init__
    elif not inspect.isroutine(component):
        fn = component.__call__ if callable(component) else None
    try:
        annotations = dict(getattr(fn, "__annotations__", None) or {})
        namespace = dict(getattr(fn, "__globals__", None) or {})
        parameters = list(inspect.signature(fn).parameters.values())
    except Exception:  # a program may hide anything behind __getattr__
        return {}, None
    variadic = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    named = {}
    rest = []  # (hint, parse function or None) of *args and **kwargs, hinted or not
    for parameter in parameters:
        hint = annotations.get(parameter.name, parameter.empty)
        if isinstance(hint, str):
            try:
                hint = eval(hint, namespace)  # as typing.get_type_hints reads it
            except Exception:  # then only its words are known
                pass
        found = None if hint is parameter.empty else _parser_for(hint)
        parse = None if found is None else _parse_function(parameter.name, *found)
        if parameter.kind in variadic:
            rest.append((hint, parse))
        elif parse is not None:
            named[parameter.name] = parse
    if not rest or rest[0][1] is None or any(hint != rest[0][0] for hint, _ in rest):
        return named, None
    for parameter in parameters:
        if parameter.kind not in variadic:
            named.setdefault(parameter.name, parser.DefaultParseValue)
    return named, rest[0][1]


@contextmanager
def typed_parsing() -> Any:
    """
    Have Fire read values as the parameters' type hints say.

    Fire reads a function's parse functions from its metadata when it
    calls it. For the duration of the call, the metadata gains a parse
    function for each parameter whose hint fireaid reads, and a default
    one for ``*args`` and ``**kwargs``. A parse function the program sets
    for a parameter with fire.decorators wins, and a default one leaves
    all parameters to the program.
    """
    original = decorators.GetMetadata
    key = decorators.FIRE_PARSE_FNS

    def GetMetadata(component: Any) -> Any:  # Fire's name
        metadata = original(component)
        parse_fns = metadata.get(key) or {"default": None, "positional": [], "named": {}}
        if parse_fns.get("default") is not None:
            return metadata
        named, rest = _parsers(component)
        if not named and rest is None:
            return metadata
        named.update(parse_fns["named"])
        return {**metadata, key: {**parse_fns, "named": named, "default": rest}}

    decorators.GetMetadata = GetMetadata
    try:
        yield
    finally:
        decorators.GetMetadata = original
