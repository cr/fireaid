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

Values are read as the parameters' type hints say, where Fire reads
every value as a Python literal.

``Fire(component, remote=True)`` adds a ``server`` command and a
``--remote`` flag, which runs a command line on such a server. Remote
control is off unless the program's own call of Fire turns it on so:
without that there is no server command, ``--remote`` is a flag like
any other, and fireaid.remote is not even imported. Nothing outside
the program, no flag and no environment variable, turns it on.
"""

from __future__ import annotations

import importlib
import inspect
import os
import re
import shlex
import sys
from collections.abc import Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from typing import Any

import fire as _fire

from . import hints as _hints

__all__ = ["Fire"]

# What fireaid stands in for while Fire runs.
_display = _fire.core.Display
_helptext = _fire.core.helptext
_formatting = _fire.core.formatting

_HELP = ["--", "--help"]
_FLAGS = ("-h", "--help")
_NOT_CALLED = (_fire.trace.INITIAL_COMPONENT, _fire.trace.INSTANTIATED_CLASS, _fire.trace.ACCESSED_PROPERTY)
_NAMED = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)

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

# The sections of Fire's help.
_SECTION_BREAK = re.compile(r"\n\n(?=[A-Z][A-Z ]*\n)")
_SYNOPSIS_NAME = re.compile(r"^(SYNOPSIS\n    \S+)")
_LISTS = ("GROUPS\n", "COMMANDS\n", "VALUES\n", "INDEXES\n", "NOTES\n")


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


def _help_command(component: Any, name: Any) -> Any:
    """
    Make the help command of a component.

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

    return help


def _add_help(component: Any, name: Any = None) -> tuple[Any, Any]:
    """Give a component the help command, as _add_member does."""
    return _add_member(component, "help", _help_command(component, name))


def _as_method(function: Any) -> Any:
    """The function as a method that ignores its instance, and looks to Fire like the function."""

    def method(self, *args, **kwargs):
        return function(*args, **kwargs)

    signature = inspect.signature(function)
    instance = inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)
    method.__signature__ = signature.replace(parameters=[instance, *signature.parameters.values()])
    method.__name__ = function.__name__
    method.__doc__ = function.__doc__
    method.__annotations__ = dict(function.__annotations__)
    return method


def _add_member(component: Any, attribute: str, function: Any) -> tuple[Any, Any]:
    """
    Give a component a command.

    Returns the component to pass to Fire, and a function to undo it.
    A dict gets a copy with the command added. A module gets it as an
    attribute, and anything else as a method of its class, for as long
    as Fire runs. A component that cannot take one is returned as it is.
    """
    if isinstance(component, dict):
        return dict(component, **{attribute: function}), None
    if inspect.ismodule(component):
        owner, member = component, function
    elif inspect.isclass(component):
        owner, member = component, _as_method(function)
    else:
        owner, member = type(component), _as_method(function)
    try:
        setattr(owner, attribute, member)
    except (AttributeError, TypeError):
        return component, None
    return component, lambda: delattr(owner, attribute)


@dataclass(frozen=True)
class Remote:
    """
    How a program is controlled remotely, for ``Fire(..., remote=Remote(...))``.

    ``allow``, if given, names the only command paths that run remotely,
    each as its words, such as ``("status", "db list")``: a command line
    must start with one of them, and a command chained onto another's
    result must be allowed too, which the command's own process holds
    it to as it runs. Allowing a group allows what is in it. It is what
    to use for a program that
    controls something physical, so that a new command stays local
    until it is allowed. ``deny`` names command paths that only run
    locally, such as ``("firmware",)`` or ``("db drop",)``, wherever they
    appear on a command line, allowed or not. ``port`` is the
    default port of server and client. ``env`` names the variable a
    client takes ``--remote`` from, by default the program's name in
    capitals followed by ``_REMOTE``. ``setup``, if given, is called with
    the server's ``--debug`` flag before it starts, for the program to set
    up its logging: the server logs through the ``fireaid.remote`` logger.
    """

    deny: tuple = ()
    port: int = 4247
    env: Any = None
    setup: Any = None
    allow: Any = None

    def __post_init__(self) -> None:
        # A single string would be taken letter by letter: an allow list
        # that allows nothing meant, or a deny list that denies nothing.
        for name in ("deny", "allow"):
            value = getattr(self, name)
            if value is None and name == "allow":
                continue
            if isinstance(value, str) or not all(isinstance(entry, str) for entry in value):
                raise TypeError(f"{name} must be a tuple of command paths, such as ('status',), not {value!r}")
            object.__setattr__(self, name, tuple(value))


def _remote_config(remote: Any) -> Any:
    """
    A Remote for the remote argument of Fire(), or None for none.

    This is the one place that decides whether a program is remote
    controlled, and only True or a Remote instance makes it so. Anything
    else that might pass for yes, such as 1, "yes" or the Remote class
    itself, is an error rather than a guess.
    """
    if remote is False or remote is None:
        return None
    if remote is True:
        remote = Remote()
    if not isinstance(remote, Remote):
        raise TypeError(f"remote must be True, False or a fireaid.Remote, not {remote!r}")
    return remote


def _remote_module() -> Any:
    """fireaid.remote, imported only once _remote_config has found remote control turned on."""
    return importlib.import_module(__name__ + ".remote")


def _server_command(remote: Remote, prog: str, variable: str, launch: Any) -> Any:
    """Make the server command."""

    def server(port=remote.port, password=None, bind="0.0.0.0", debug=False):
        module = _remote_module()
        if remote.setup is not None:
            remote.setup(bool(debug))
        try:
            module.serve(launch, remote.deny, remote.allow, prog, variable, port, password, bind, bool(debug))
        except module.RemoteError as e:
            print(f"{prog}: {e}", file=sys.stderr)
            raise SystemExit(e.code) from None

    secret = _remote_module().password_variable(prog)
    local = ""
    if remote.allow is not None:
        names = ", ".join(remote.allow) or "none"
        local += f"\n\n        Only these commands run remotely: {names}."
    if remote.deny:
        names = ", ".join(remote.deny)
        local += f"\n\n        These commands only run locally: {names}."
    server.__doc__ = f"""Serve the commands of this program to other computers.

        A client runs a command here by adding --remote [PASSWORD@]HOST[:PORT]
        anywhere on its command line, or by setting {variable}. An empty
        --remote= runs the command locally. Without a password, anyone who
        can reach the port may run commands; with one, clients are
        authenticated by HMAC challenge-response. The connection is not
        encrypted either way. {secret} gives the password where
        --password does not, out of sight of other users.

        Each command runs in a new process of this program, as if typed
        here. Its output, input and exit code are relayed, and it sees a
        terminal where the client has one: colour, progress bars, the
        terminal width, prompts and Ctrl-C work as they do locally.
        One command runs at a time; a client that comes meanwhile waits a
        few seconds, then is told the server is busy. Where Ctrl-C is a key for the server's terminal,
        Enter ~ . disconnects.{local}

        Args:
            port: The TCP port to listen on, 0 for any free one.
            password: The password clients must give; none by default, or {secret}.
            bind: The address to listen on: all of this computer's by default,
                127.0.0.1 for clients on this computer only.
            debug: Log each client, its command line and how it ended.
        """
    # Types for the help, as objects: this module's annotations are strings.
    server.__annotations__ = {"port": int, "password": str, "bind": str, "debug": bool}
    return server


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


def _unstripped(stream: Any) -> Any:
    """
    The stream colorama wraps, where it would only strip colour from it.

    On Windows, Fire has colorama wrap stdout and stderr, which strips
    colour from what is not a terminal, FORCE_COLOR or not. On a legacy
    console it turns colour into console calls instead: there the
    wrapper stays.
    """
    convertor = getattr(stream, "_StreamWrapper__convertor", None)  # colorama's StreamWrapper
    if convertor is not None and getattr(convertor, "strip", False) and not getattr(convertor, "convert", True):
        return getattr(convertor, "wrapped", stream)
    return stream


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

    def __init__(self, color_usage: bool, color_help: bool, conventional: bool, remote: Any) -> None:
        self.incomplete = None
        self.color_usage = color_usage
        self.color_help = color_help
        self.conventional = conventional  # help was asked for in fireaid's syntax
        self.remote = remote  # (prog, variable, deny, allow) of a remote-controlled program

    def __getattr__(self, name: str) -> Any:
        return getattr(_helptext, name)

    def HelpText(self, component: Any, trace: Any = None, verbose: bool = False) -> str:
        if trace is None or trace.show_help or trace.HasError() or _called(trace):
            text = None
            if self.conventional and trace is not None and trace.show_help and not trace.HasError():
                text = _class_help(component, trace, verbose)
            if text is None:
                text = _helptext.HelpText(component, trace=trace, verbose=verbose)
            if self.remote is not None:
                text = _with_remote_flag(text, trace, *self.remote, color=self.color_help)
            return _color_help(text) if self.color_help else text
        self.incomplete = trace
        return self.UsageText(component, trace=trace, verbose=verbose)

    def UsageText(self, component: Any, trace: Any = None, verbose: bool = False) -> str:
        text = _helptext.UsageText(component, trace=trace, verbose=verbose)
        if trace is not None and trace is self.incomplete:
            text = _without_separator(text, trace)
        return _color_usage(text) if self.color_usage else text


def _section(sections: Sequence[str], heading: str) -> Any:
    """The index of the section of help under heading, or None."""
    return next((i for i, s in enumerate(sections) if s.startswith(heading + "\n")), None)


def _with_remote_flag(text: str, trace: Any, prog: str, variable: str, deny: Sequence[str], allow: Any,
                      color: bool) -> str:
    """
    Add --remote to the flags in the help for what can run remotely: the
    program, its groups and commands, other than help, the server and
    what the program denies or, with an allow list, does not allow.
    """
    try:
        command = trace.GetCommand(include_separators=False)
    except AttributeError:
        return text
    words = shlex.split(command)[1:]
    if words[:1] == ["help"] or _remote_module().refusal(words, deny, allow) is not None:
        return text
    placeholder = "[PASSWORD@]HOST[:PORT]"
    entry = (f"    --remote={_placeholder(placeholder) if color else placeholder}\n"
             f"        Run the command line on HOST, where `{prog} server` is running;\n"
             f"        see `{prog} help server`. {variable} gives it by default.")
    sections = _SECTION_BREAK.split(text)
    flags = _section(sections, "FLAGS")
    if flags is not None:
        sections[flags] += "\n" + entry
        return "\n\n".join(sections)
    at = next((i for i, s in enumerate(sections) if s.startswith(_LISTS)), len(sections))
    sections.insert(at, "FLAGS\n" + entry)
    synopsis = re.compile(rf"^(SYNOPSIS\n    {re.escape(trace.GetCommand(include_separators=True))})( |$)")
    return "\n\n".join(synopsis.sub(r"\1 <flags>\2", s, count=1) for s in sections)


def _class_help(component: Any, trace: Any, verbose: bool) -> Any:
    """
    Help for a class: the commands of an instance, and the constructor's flags.

    Fire does not instantiate a class to show help for it, and so shows
    only its constructor's flags. Here the class is instantiated without
    arguments, where it can be, for the help of the instance, into which
    the flags are inserted. Returns None where Fire's help is to stand.
    """
    if not inspect.isclass(component):
        return None
    try:
        if trace.GetLastHealthyElement().component is not component:
            return None
        parameters = inspect.signature(component).parameters.values()
    except (AttributeError, TypeError, ValueError):
        return None
    if any(p.default is p.empty and p.kind not in (p.VAR_POSITIONAL, p.VAR_KEYWORD) for p in parameters):
        return None
    try:
        instance = component()
    except Exception:
        return None
    text = _helptext.HelpText(instance, trace=trace, verbose=verbose)
    fires = _SECTION_BREAK.split(_helptext.HelpText(component, trace=trace, verbose=verbose))
    flags = _section(fires, "FLAGS")
    if flags is None:
        return text
    sections = _SECTION_BREAK.split(text)
    after = _section(sections, "DESCRIPTION")
    if after is None:
        after = _section(sections, "SYNOPSIS") or 0
    sections.insert(after + 1, fires[flags])
    return "\n\n".join(_SYNOPSIS_NAME.sub(r"\1 <flags>", s, count=1) for s in sections)


def _without_separator(text: str, trace: Any) -> str:
    """
    Take the separator out of the usage of a freshly instantiated class.

    Fire puts it there in case the next argument is for the constructor,
    but a constructor only takes flags, so the commands follow directly.
    """
    try:
        element = trace.GetLastHealthyElement()
        if element._action != _fire.trace.INSTANTIATED_CLASS or not element.HasCapacity():
            return text
        separator = trace.separator
    except AttributeError:
        return text
    sep = re.escape(separator)
    text = re.sub(rf"^(Usage: .*?) {sep} (?=<)", r"\1 ", text, count=1, flags=re.MULTILINE)
    return re.sub(rf"^(  .*?) {sep} (?=--help$)", r"\1 ", text, count=1, flags=re.MULTILINE)


# Obtain the signature from the installed Fire version instead of
# duplicating it here. This makes fireaid less dependent on Fire's
# particular API version.
_FIRE_SIGNATURE = inspect.signature(_fire.Fire)


# fireaid.Fire keeps fire.Fire's docstring, set below. What it does on
# top: all arguments other than ``component`` and ``command`` are passed
# through unchanged. The command, by default taken from sys.argv, is
# normalized first, making this work:
#
#     fire.Fire(MyCLI, command=["foo", "--help"])
#     fire.Fire(MyCLI, command="help foo")
#
# Values are read as the parameters' type hints say. With ``remote``,
# True or a ``fireaid.Remote``, the program gets a ``server`` command
# and a ``--remote [PASSWORD@]HOST[:PORT]`` flag. Remote control is off
# otherwise: remote is keyword-only, False by default, and everything
# remote below depends on what _remote_config makes of it.
def Fire(*args: Any, remote: Any = False, **kwargs: Any) -> Any:
    remote = _remote_config(remote)
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
    undo_server = None
    remote_help = None
    enforce_allow = None
    if remote is not None:
        module = _remote_module()
        prog = arguments.get("name") or os.path.basename(sys.argv[0] if sys.argv else "")
        variable = module.environment_variable(prog, remote.env)
        if inspect.isroutine(component):
            raise ValueError("remote=True needs a program with commands, not a single function")
        if _member(component, "server")[0]:
            raise ValueError("remote=True needs the name 'server'; the program already has it")
        try:
            target, command = module.take_flag(command, prog, remote.port, variable)
            if target is not None:
                code = module.run_client(target, command)
                if code:
                    raise SystemExit(code)
                return None
        except module.RemoteError as e:
            print(f"{prog}: {e}", file=sys.stderr)
            raise SystemExit(e.code) from None
        arguments["command"] = command
        launch = module.Launch.capture()
        component, undo_server = _add_member(component, "server", _server_command(remote, prog, variable, launch))
        arguments["component"] = component
        remote_help = (prog, variable, remote.deny, remote.allow)
        if remote.allow is not None and os.environ.get(module.CHILD_VARIABLE):
            # In the command's own process on the server: hold the command to
            # what is allowed as it actually runs, chained calls included,
            # where the server's check of the command line as text cannot.
            enforce_allow = remote.allow, prog

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
        return _fire_with(display, *bound.args, remote_help=remote_help, enforce_allow=enforce_allow, **bound.kwargs)
    finally:
        if undo is not None:
            undo()
        if undo_server is not None:
            undo_server()


@contextmanager
def _allowed_calls(allow: Sequence[str], prog: str) -> Any:
    """
    Hold a running command to the allowed paths, by what Fire takes.

    The server's refusal reads the command line as text and so sees only
    where it starts. Here, in the command's own process, each command
    word is checked before Fire looks it up, so that not even a
    property's code runs for a word refused: navigating towards or
    within an allowed path is let through, a class on the way included,
    which Fire instantiates; but once a routine has been called, a
    further command word is a chain onto its result, and refused. help
    and server are fireaid's own and left alone.

    Only the command's own run is held so: what the command does with
    Fire itself, in a nested call, is its own business. A refusal ends
    the run at once, without Fire's usage text, which would read every
    member, properties included, to list them, and says so as the server
    would, but for the server's address, which only the client knows.
    """
    allowed = [tuple(w.replace("-", "_") for w in entry.split()) for entry in allow]
    run, get_member, call = _fire.core._Fire, _fire.core._GetMember, _fire.core._CallAndUpdateTrace
    path: list[str] = []
    called = [False]
    depth = [0]  # Fire runs under way: 1 is the command's own

    def refuse(words: str) -> None:
        print(f"{prog}: {_remote_module().not_available(words)}", file=sys.stderr)
        raise _fire.core.FireExit(2, None)

    def _Fire(*args: Any, **kwargs: Any) -> Any:  # noqa: N802 - Fire's name
        depth[0] += 1
        try:
            return run(*args, **kwargs)
        finally:
            depth[0] -= 1

    def _GetMember(component: Any, args: Any) -> Any:  # noqa: N802 - Fire's name
        if depth[0] == 1:
            word = args[0].replace("-", "_")
            if called[0]:
                refuse(args[0])  # a command word after a command ran: a chain onto its result
            here = (*path, word)
            if here[0] not in ("help", "server") and not any(here[: len(a)] == a[: len(here)] for a in allowed if a):
                refuse(" ".join(here))
            path.append(word)
        return get_member(component, args)

    def _CallAndUpdateTrace(component: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: N802 - Fire's name
        # Calling a routine once a command word was taken runs the command.
        # Instantiating a class does not: it is the program, or a group on the way.
        if depth[0] == 1 and path and not inspect.isclass(component):
            called[0] = True
        return call(component, *args, **kwargs)

    _fire.core._Fire, _fire.core._GetMember, _fire.core._CallAndUpdateTrace = _Fire, _GetMember, _CallAndUpdateTrace
    try:
        yield
    finally:
        _fire.core._Fire, _fire.core._GetMember, _fire.core._CallAndUpdateTrace = run, get_member, call


def _fire_with(display: Any, *args: Any, remote_help: Any = None, enforce_allow: Any = None, **kwargs: Any) -> Any:
    """
    Call fire.Fire() with stand-ins for its display of help and its parsing.

    On a terminal, usage text is coloured, and so is help that was asked
    for in conventional syntax. Help in Fire's native syntax is Fire's.
    Where colour is on, colorama does not strip it. Values are read as
    the parameters' type hints say.
    """
    color = _use_color()
    # What to put back afterwards: a command may itself call Fire, within this call.
    streams = sys.stdout, sys.stderr
    previous = _fire.core.Display, _fire.core.helptext
    conventional = display is _display_stdout
    # What Fire marks as bold are headings, as underlined placeholders.
    marks = {"Bold": _heading, "Underline": _placeholder} if color and conventional else {}
    originals = {name: getattr(_formatting, name) for name in marks}

    helptext = _HelpText(color_usage=color, color_help=bool(marks), conventional=conventional, remote=remote_help)
    _fire.core.helptext = helptext
    _fire.core.Display = display
    for name, stand_in in marks.items():
        setattr(_formatting, name, stand_in)
    if color:
        sys.stdout, sys.stderr = map(_unstripped, streams)
    try:
        with _hints.typed_parsing(), \
                (_allowed_calls(*enforce_allow) if enforce_allow is not None else nullcontext()):
            result = _fire.Fire(*args, **kwargs)
    finally:
        sys.stdout, sys.stderr = streams
        _fire.core.Display, _fire.core.helptext = previous
        for name, original in originals.items():
            setattr(_formatting, name, original)

    if helptext.incomplete is not None:
        # Fire has printed the usage in place of a result.
        raise _fire.core.FireExit(2, helptext.incomplete)
    return result


# Make introspection of fireaid.Fire look like fire.Fire rather than
# showing the wrapper's (*args, **kwargs) implementation.
Fire.__signature__ = _FIRE_SIGNATURE.replace(
    parameters=[
        *_FIRE_SIGNATURE.parameters.values(),
        inspect.Parameter("remote", inspect.Parameter.KEYWORD_ONLY, default=False),
    ]
)
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

    fireaid's own __version__ is looked up when first asked for:
    importlib.metadata would take a noticeable part of every program's
    start.
    """
    if name == "__version__":
        from importlib import metadata

        try:
            version = metadata.version("fireaid")
        except metadata.PackageNotFoundError:
            version = "unknown"
        globals()["__version__"] = version
        return version
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
