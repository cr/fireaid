"""
fireaid
=======

A transparent wrapper around Google Python Fire that adds conventional
command-line help syntax.

Instead of:

    import fire

use:

    import fireaid as fire

Existing code can continue to use:

    fire.Fire(...)

and attributes exposed by the original ``fire`` package remain available.

In addition to Fire's native:

    tool foo -- --help

fireaid accepts:

    tool --help
    tool -h
    tool foo --help
    tool foo -h
    tool help
    tool help foo
    tool help foo bar
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import Sequence
from typing import Any

import fire as _fire


def _normalize_help(argv: Sequence[str]) -> list[str]:
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

    Only exact ``-h`` and ``--help`` tokens are interpreted. Thus values
    such as ``--help=false`` or ``--name=--help`` are left alone.
    """
    argv = list(argv)

    if not argv:
        return argv

    # Native Fire syntax is already being used. Do not interfere.
    if "--" in argv:
        return argv

    # Git-style help:
    #
    #     tool help
    #     tool help foo
    #     tool help foo bar
    #
    if argv[0] == "help":
        return argv[1:] + ["--", "--help"]

    # Conventional Unix help:
    #
    #     tool --help
    #     tool foo --help
    #     tool foo bar -h
    #
    # Remove only the exact help token and append Fire's own help flag
    # behind its separator.
    for i, arg in enumerate(argv):
        if arg in ("-h", "--help"):
            return argv[:i] + argv[i + 1:] + ["--", "--help"]

    return argv


# Obtain the signature from the installed Fire version instead of
# duplicating it here. This makes fireaid less dependent on Fire's
# particular API version.
_FIRE_SIGNATURE = inspect.signature(_fire.Fire)


def Fire(*args: Any, **kwargs: Any) -> Any:
    """
    Call fire.Fire(), adding conventional command-line help handling.

    All arguments other than ``command`` are passed through unchanged.
    If ``command`` is not explicitly supplied, sys.argv is normalized
    and supplied to Fire.

    Explicit list/tuple commands are normalized too, making this work:

        fire.Fire(MyCLI, command=["foo", "--help"])

    Other command representations are passed through unchanged.
    """
    bound = _FIRE_SIGNATURE.bind_partial(*args, **kwargs)

    if "command" in bound.arguments:
        command = bound.arguments["command"]

        if isinstance(command, (list, tuple)):
            bound.arguments["command"] = _normalize_help(command)

    else:
        # This is what Fire would normally obtain implicitly from
        # sys.argv. Supplying it explicitly lets us normalize help first.
        bound.arguments["command"] = _normalize_help(sys.argv[1:])

    return _fire.Fire(*bound.args, **bound.kwargs)


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


# Expose the wrapped Fire version. This is more useful to callers than
# a separate wrapper version when they inspect fire.__version__.
__version__ = getattr(_fire, "__version__", None)
