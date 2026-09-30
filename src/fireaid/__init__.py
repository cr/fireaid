"""
fireaid
=======

A transparent wrapper around Google Python Fire that rounds off its
command-line help syntax.

Instead of:

    import fire

use:

    import fireaid as fire

Existing code can continue to use:

    fire.Fire(...)

and attributes exposed by the original ``fire`` package remain available.

Fire itself accepts:

    tool foo -- --help
    tool foo --help
    tool foo -h

fireaid additionally accepts Git-style help:

    tool help
    tool help foo
    tool help foo bar

and makes ``--help`` and ``-h`` print the help text to stdout, without
Fire's INFO banner or pager, including for functions that take ``**kwargs``.

An incomplete command, such as plain ``tool``, prints Fire's usage text
and exits with an error, where Fire shows the full help as a success.

On a terminal, usage text and help are coloured.
"""

from __future__ import annotations

import inspect
import os
import re
import shlex
import sys
from collections.abc import Sequence
from importlib import metadata as _metadata
from typing import Any

import fire as _fire

__all__ = ["Fire"]

_HELP = ["--", "--help"]
_FLAGS = ("-h", "--help")
_NOT_CALLED = ("Initial component", "Instantiated class", "Accessed property")
_NAMED = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)


def _member(component: Any, name: str) -> tuple[bool, Any]:
    """
    Look up a member the way Fire does: by name, then with ``-`` as ``_``.
    """
    for key in (name, name.replace("-", "_")):
        if isinstance(component, dict):
            if key in component:
                return True, component[key]
        else:
            try:
                return True, getattr(component, key)
            except Exception:
                pass
    return False, None


def _resolve(component: Any, path: Sequence[str]) -> tuple[Any, bool]:
    """
    Follow ``path`` from ``component`` as far as it names members.

    Returns the component reached and whether it is known to be the
    target of the command. It is when the whole path was followed, or
    when a function was reached, since the rest are then its arguments.
    Members that only exist on an instance cannot be followed; the class
    is returned as not known to be the target.
    """
    for name in path:
        if inspect.isroutine(component):
            return component, True
        found, member = _member(component, name)
        if not found:
            return component, False
        component = member
    return component, True


def _accepts(target: Any, flag: str) -> bool:
    """
    Tell whether Fire would pass ``flag`` to ``target`` as an argument.

    ``--help`` is an argument of a callable with a ``help`` parameter.
    ``-h`` is Fire's shortcut for any parameter starting with ``h``.
    """
    if not (inspect.isclass(target) or inspect.isroutine(target)):
        return False
    try:
        parameters = inspect.signature(target).parameters.values()
    except (TypeError, ValueError):
        return False
    names = [p.name for p in parameters if p.kind in _NAMED]
    if flag == "--help":
        return "help" in names
    return any(name.startswith("h") for name in names)


def _normalize_help(argv: Sequence[str], component: Any, ours: bool) -> list[str]:
    """
    Translate conventional help syntax into Python Fire's native syntax.

    Examples:

        --help
            -> -- --help

        foo --help
            -> foo -- --help

        foo -h
            -> foo -- --help

        help
            -> -- --help

        help foo bar
            -> foo bar -- --help

    If an explicit ``--`` separator is already present, the command is
    assumed to use native Fire syntax and is left untouched.

    ``help`` is only interpreted as the first word, and only if it is
    ``ours``: the component is not a function and has no ``help`` member
    of its own. ``help --help`` is help for the help command itself.

    ``-h`` and ``--help`` are only interpreted as the last word, and
    only if the command they follow does not take them as an argument.
    Where that command cannot be determined, ``-h`` is left to Fire.
    """
    argv = list(argv)

    if not argv or "--" in argv:
        return argv

    flag = argv[-1]

    if argv[0] == "help" and ours:
        if flag not in _FLAGS:
            return argv[1:] + _HELP
        if len(argv) > 2 or not _member(component, "help")[0]:
            return argv[1:-1] + _HELP

    if flag not in _FLAGS:
        return argv

    target, known = _resolve(component, argv[:-1])
    if _accepts(target, flag) or (flag == "-h" and not known):
        return argv
    return argv[:-1] + _HELP


def _help_command(component: Any, name: Any) -> tuple[Any, Any]:
    """
    Make the help command of a component, as a function and as a method.

    Command lines starting with ``help`` are translated before Fire sees
    them, so this is mostly there for Fire to list with the component's
    other commands. It is what runs if Fire is given the command in its
    native syntax.
    """

    def help(*command):
        """Show help for the program, or for a command.

        Args:
            command: The command to show help for, as it would be run.
        """
        words = [str(word) for word in command]
        return _fire_with(_display_stdout, component, command=words + _HELP, name=name)

    def method(self, *command):
        return help(*command)

    method.__name__ = help.__name__
    method.__doc__ = help.__doc__
    return help, method


def _add_help(component: Any, name: Any = None) -> tuple[Any, Any]:
    """
    Give a component a help command.

    Returns the component to pass to Fire, and a function to undo it.
    A dict gets a copy with the command added. A module gets it as an
    attribute, and anything else as a method of its class, for as long
    as Fire runs. A component that cannot take one is returned as it is.
    """
    help, method = _help_command(component, name)
    if isinstance(component, dict):
        return dict(component, help=help), None
    if inspect.ismodule(component):
        owner, member = component, help
    elif inspect.isclass(component):
        owner, member = component, method
    else:
        owner, member = type(component), method
    try:
        setattr(owner, "help", member)
    except (AttributeError, TypeError):
        return component, None
    return component, lambda: delattr(owner, "help")


# What colours what, as the parameters of an ANSI escape sequence.
# Headings are bold light blue, labels within the text light blue, the
# names of commands and flags light green, placeholders yellow.
_HEADING = "1;94"
_LABEL = "94"
_NAME = "92"
_PLACEHOLDER = "33"

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_FLAG_LINE = re.compile(r"^(    )((?:-\w, )?--[\w-]+)", re.MULTILINE)
_NAME_LINE = re.compile(r"^(     )(\S+)$")
_USAGE_ITEMS = re.compile(r"^(  )([a-z ]+:)( +)(.*)$")
_USAGE_ITEM = re.compile(r"[^\s|]+")
_USAGE_MORE = re.compile(r"^( {20,})(.*)$")


def _paint(style: str, text: str) -> str:
    return f"\x1b[{style}m{text}\x1b[0m"


def _heading(text: str) -> str:
    # Fire also marks some placeholders as bold. They have their colour.
    return text if "\x1b[" in text else _paint(_HEADING, text)


def _placeholder(text: str) -> str:
    return _paint(_PLACEHOLDER, text)


def _use_color() -> bool:
    """
    Tell whether help and usage are to be coloured.

    They are on a terminal that has colours, that is if neither stdout
    nor stderr is redirected, and the terminal's description says so.
    NO_COLOR switches colour off, FORCE_COLOR on.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    try:
        if not (sys.stdout.isatty() and sys.stderr.isatty()):
            return False
    except (AttributeError, ValueError):
        return False
    return _terminal_has_colors()


def _terminal_has_colors() -> bool:
    """
    Tell whether the terminal named by TERM has colours.

    The terminfo database knows, where there is one. A terminal it does
    not know is taken to be a new one rather than an old one. Windows
    has neither TERM nor terminfo, and a console that has colours.
    """
    term = os.environ.get("TERM")
    if os.name == "nt":
        return term != "dumb"
    if not term or term == "dumb":
        return False
    try:
        import curses

        curses.setupterm(term=term, fd=sys.stdout.fileno())
        return curses.tigetnum("colors") >= 8
    except Exception:
        return True


def _color_help(text: str) -> str:
    """
    Colour what Fire does not mark in its help text: the names of flags,
    and the names in the lists of commands, groups and values.

    Headings and placeholders are marked by Fire, and coloured by the
    stand-ins for its Bold and Underline.
    """
    lines = []
    section = ""
    for line in text.split("\n"):
        if line and not line.startswith(" "):
            section = _ANSI.sub("", line)
        elif section == "FLAGS":
            line = _FLAG_LINE.sub(lambda m: m.group(1) + _paint(_NAME, m.group(2)), line)
        elif section in ("COMMANDS", "GROUPS", "VALUES", "INDEXES"):
            line = _NAME_LINE.sub(lambda m: m.group(1) + _paint(_NAME, m.group(2)), line)
        lines.append(line)
    return "\n".join(lines)


def _color_usage(text: str) -> str:
    """
    Colour Fire's usage text, which comes without any marks.
    """

    def items(text: str) -> str:
        return _USAGE_ITEM.sub(lambda m: _paint(_NAME, m.group()), text)

    lines = []
    listing = False
    for line in text.split("\n"):
        found = _USAGE_ITEMS.match(line)
        more = _USAGE_MORE.match(line) if listing else None
        if line.startswith("Usage: "):
            line = _heading("Usage:") + line[len("Usage:"):]
        elif found:
            indent, label, gap, rest = found.groups()
            line = indent + _paint(_LABEL, label) + gap + items(rest)
        elif more:
            line = more.group(1) + items(more.group(2))
        listing = bool(found or more)
        lines.append(line)
    return "\n".join(lines)


def _display_stdout(lines: Sequence[str], out: Any = None) -> None:
    """
    Stand in for fire.core.Display, printing to stdout instead of stderr,
    and not through a pager.
    """
    sys.stdout.write("\n".join(lines) + "\n")


def _display_unpaged(lines: Sequence[str], out: Any) -> None:
    """
    Stand in for fire.core.Display, paging only what Fire sends to stderr.

    What Fire sends to stdout is the text for a command line that names
    a group rather than a command. Its native help goes to stderr.
    """
    if out is sys.stdout:
        _display_stdout(lines)
    else:
        _display(lines, out=out)


def _called(trace: Any) -> bool:
    """
    Tell whether the last step of a Fire trace was a function call.

    If the trace does not say, assume so, which leaves things to Fire.
    """
    try:
        action = trace.elements[-1]._action
    except (AttributeError, IndexError):
        return True
    return action not in _NOT_CALLED


class _HelpText:
    """
    Stand in for the fire.helptext module, as fire.core sees it.

    Fire prints the full help for a command line that names a group
    rather than a command, and calls it a success. This makes it the
    usage text, and keeps the trace, for the caller to call it an error.

    Fire prints the same for an object returned by a function. That is
    left alone: the command was complete.
    """

    def __init__(self, color_usage: bool, color_help: bool) -> None:
        self.incomplete = None
        self.color_usage = color_usage
        self.color_help = color_help

    def __getattr__(self, name: str) -> Any:
        return getattr(_helptext, name)

    def HelpText(self, component: Any, trace: Any = None, verbose: bool = False) -> str:
        if trace is None or trace.show_help or trace.HasError() or _called(trace):
            text = _helptext.HelpText(component, trace=trace, verbose=verbose)
            return _color_help(text) if self.color_help else text
        self.incomplete = trace
        return self.UsageText(component, trace=trace, verbose=verbose)

    def UsageText(self, component: Any, trace: Any = None, verbose: bool = False) -> str:
        text = _helptext.UsageText(component, trace=trace, verbose=verbose)
        return _color_usage(text) if self.color_usage else text


_display = getattr(_fire.core, "Display", None)
_helptext = getattr(_fire.core, "helptext", None)
_formatting = getattr(_fire.core, "formatting", None)

# Obtain the signature from the installed Fire version instead of
# duplicating it here. This makes fireaid less dependent on Fire's
# particular API version.
_FIRE_SIGNATURE = inspect.signature(_fire.Fire)


def Fire(*args: Any, **kwargs: Any) -> Any:
    """
    Call fire.Fire(), adding conventional command-line help handling.

    All arguments other than ``component`` and ``command`` are passed
    through unchanged. The command, by default taken from sys.argv, is
    normalized first, making this work:

        fire.Fire(MyCLI, command=["foo", "--help"])
        fire.Fire(MyCLI, command="help foo")
    """
    bound = _FIRE_SIGNATURE.bind_partial(*args, **kwargs)
    arguments = bound.arguments

    if arguments.get("component") is None:
        # Fire would look at its caller, which is this function. Supply
        # the context of our own caller instead.
        frame = sys._getframe(1)
        arguments["component"] = {**frame.f_globals, **frame.f_locals}

    command = arguments.get("command")
    if command is None:
        command = sys.argv[1:]
    elif isinstance(command, str):
        command = shlex.split(command)

    if not isinstance(command, (list, tuple)):
        # Not a valid command. Let Fire report it.
        return _fire.Fire(*bound.args, **bound.kwargs)

    component = arguments["component"]
    ours = not (inspect.isroutine(component) or _member(component, "help")[0])
    undo = None
    if ours:
        arguments["component"], undo = _add_help(component, arguments.get("name"))

    try:
        arguments["command"] = _normalize_help(command, arguments["component"], ours)
        if arguments["command"] == list(command):
            display = _display_unpaged
        else:
            # Help was asked for in conventional syntax: it belongs on stdout.
            display = _display_stdout
        return _fire_with(display, *bound.args, **bound.kwargs)
    finally:
        if undo is not None:
            undo()


def _fire_with(display: Any, *args: Any, **kwargs: Any) -> Any:
    """
    Call fire.Fire() with a stand-in for its display of help.

    On a terminal, usage text is coloured, and so is help that was asked
    for in conventional syntax. Help in Fire's native syntax is Fire's.
    """
    if _display is None or _helptext is None:
        return _fire.Fire(*args, **kwargs)

    color = _use_color()
    marks = {}
    if color and display is _display_stdout:
        # What Fire marks as bold are headings, as underlined placeholders.
        marks = {"Bold": _heading, "Underline": _placeholder}
        if not all(hasattr(_formatting, name) for name in marks):
            marks = {}
    found = {name: getattr(_formatting, name) for name in marks}

    helptext = _HelpText(color_usage=color, color_help=bool(marks))
    _fire.core.helptext = helptext
    _fire.core.Display = display
    for name, stand_in in marks.items():
        setattr(_formatting, name, stand_in)
    try:
        result = _fire.Fire(*args, **kwargs)
    finally:
        _fire.core.Display = _display
        _fire.core.helptext = _helptext
        for name, original in found.items():
            setattr(_formatting, name, original)

    if helptext.incomplete is not None:
        # Fire has printed the usage in place of a result.
        raise _fire.core.FireExit(2, helptext.incomplete)
    return result


# Make introspection of fireaid.Fire look like fire.Fire rather than
# showing the wrapper's (*args, **kwargs) implementation.
Fire.__signature__ = _FIRE_SIGNATURE
Fire.__wrapped__ = _fire.Fire
Fire.__doc__ = _fire.Fire.__doc__


def __getattr__(name: str) -> Any:
    """
    Proxy attributes not defined by fireaid to the real fire package.

    Examples:

        fireaid.core
        fireaid.decorators
        fireaid.inspectutils
        fireaid.trace
    """
    return getattr(_fire, name)


def __dir__() -> list[str]:
    """
    Return the combined namespace of fireaid and the wrapped fire module.
    """
    return sorted(set(globals()) | set(dir(_fire)))


# Make the submodules Fire has loaded importable under our name too:
#
#     import fireaid.core
#     from fireaid.core import FireExit
#
for _name, _module in list(sys.modules.items()):
    if _name.startswith("fire.") and not _name.endswith("__main__"):
        sys.modules.setdefault(__name__ + _name[len("fire"):], _module)

try:
    __version__ = _metadata.version("fireaid")
except _metadata.PackageNotFoundError:
    __version__ = "unknown"
