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
Fire's INFO banner, including for functions that take ``**kwargs``.
"""

from __future__ import annotations

import inspect
import shlex
import sys
from collections.abc import Sequence
from importlib import metadata as _metadata
from typing import Any

import fire as _fire

__all__ = ["Fire"]

_HELP = ["--", "--help"]
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


def _normalize_help(argv: Sequence[str], component: Any) -> list[str]:
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

    ``help`` is only interpreted as the first word, and only if the
    component is not a function and has no ``help`` member of its own.

    ``-h`` and ``--help`` are only interpreted as the last word, and
    only if the command they follow does not take them as an argument.
    Where that command cannot be determined, ``-h`` is left to Fire.
    """
    argv = list(argv)

    if not argv or "--" in argv:
        return argv

    if argv[0] == "help":
        if inspect.isroutine(component) or _member(component, "help")[0]:
            return argv
        return argv[1:] + _HELP

    flag = argv[-1]
    if flag not in ("-h", "--help"):
        return argv

    target, known = _resolve(component, argv[:-1])
    if _accepts(target, flag) or (flag == "-h" and not known):
        return argv
    return argv[:-1] + _HELP


def _display_stdout(lines: Sequence[str], out: Any = None) -> None:
    """
    Stand in for fire.core.Display, printing to stdout instead of stderr.
    """
    _display(lines, out=sys.stdout)


_display = getattr(_fire.core, "Display", None)

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

    arguments["command"] = _normalize_help(command, arguments["component"])

    if arguments["command"] == list(command) or _display is None:
        return _fire.Fire(*bound.args, **bound.kwargs)

    # Help was asked for in conventional syntax, so it belongs on stdout.
    _fire.core.Display = _display_stdout
    try:
        return _fire.Fire(*bound.args, **bound.kwargs)
    finally:
        _fire.core.Display = _display


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
