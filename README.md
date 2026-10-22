# fireaid

*Familiar help, typed values and remote control for Python Fire.*

[Python Fire](https://pypi.org/project/fire/) turns any Python
function, class or module into a command-line tool with a single line
of code. `fireaid` is a thin wrapper around it that adds the help
conventions people know from tools like `git`: a `help` command,
`--help` printed straight to the terminal, a short usage message when
a command is incomplete, and colour that makes it all easy to read.
It gives parameters the values their type hints say, where Fire takes
every value for a Python literal, and a program that asks for it with
`remote=True`, and only then, can run its commands on another computer.
Everything else is Fire, unchanged.

All you need is:

    pip install fireaid

    import fireaid as fire

    fire.Fire(MyTool)

## Installation

From [PyPI](https://pypi.org/project/fireaid/):

    pip install fireaid

As a dependency in another project's `pyproject.toml`:

    dependencies = ["fireaid"]

The latest state of the repository:

    pip install git+https://github.com/cr/fireaid

From the project directory:

    pip install .

For development:

    pip install -e .

fireaid requires Fire 0.2.0 or later, the first version to accept
`--help` without a separator, and Fire 0.4.0 for `python -m fireaid`.
On Python 3.13 and later, it requires Fire 0.7.0, the first that runs
there.

## Usage

Change:

    import fire

to:

    import fireaid as fire

Existing code such as:

    fire.Fire(MyCLI)

continues to work, as do `import fireaid.core` and `python -m fireaid`.

## Type hints

Fire parses every value on the command line as a Python literal where
it can, whatever the parameter's type hint: `--name 42` gives an `int`,
`--name 1e3` a `float`, `--name [1,2]` a list. fireaid reads the hint
instead, in a function, a method or a class constructor, and gives the
parameter what the hint says or a usage error:

    def set(self, name: str, count: int, since: datetime.date):
        ...

    tool set 007 0x10 2026-10-15    # name='007', count=16, since=date(2026, 10, 15)
    tool set 007 1e3 2026-10-15     # ERROR: count: '1e3' is not an integer

The hints fireaid reads:

- `str`: the string as typed, quotes and all.
- `int`: with `0x`, `0o` and `0b` prefixes and `_` separators, as
  `int(text, 0)` reads it; `float`, `complex`, `Decimal`, `Fraction`.
- `bool`: true, false, yes, no, on, off, 1 or 0, in any case. A bare
  `--flag` is true and `--noflag` false, as in Fire.
- `Path`, `UUID`, and the `ipaddress` addresses, networks and
  interfaces.
- `date`, `datetime` and `time`, in ISO 8601 as the Python version's
  `fromisoformat` reads it: `2026-10-15`, `2026-10-15T12:30`, `12:30`.
- `timedelta`: a number of seconds, units as in `1h30m`, `2d`, `1.5h`
  or `250ms`, or the clock form `1:30:00` that `str()` prints.
- An `Enum`: a member by name, in any case, or by value.
- `Optional[T]` and `T | None`: the text `None` is `None`, anything
  else is read as `T`. A `NewType` and a subclass of any of the above
  are read as the type they are made of.

The hint of `*args` is read for each of its values, and the hint of
`**kwargs` for each flag it takes, unless the two differ.

Other hints, and parameters without one, are left to Fire, and so is
a parameter whose parse function the program sets with
`fire.decorators.SetParseFn` or `SetParseFns`.

## Help

Python Fire accepts:

    tool foo -- --help
    tool foo --help
    tool foo -h

fireaid additionally supports Git-style help:

    tool help
    tool help foo
    tool help foo bar

`help` is a command like any other: Fire lists it with the program's
commands in the usage text and in the help, and `tool help help` shows
help for it.
To that end, fireaid adds the command to the program for as long as
Fire runs: to its class, to a module as an attribute, or to a copy if
the program is a dict.

A program that is a class, as in `fire.Fire(MyTool)`, gets the help of
an instance. Fire does not instantiate a class to show its help, and so
lists only the flags of its constructor. fireaid instantiates it, without
arguments, and lists its groups, commands and values, with the
constructor's flags among them. A class whose constructor needs
arguments keeps Fire's help.

With `--help` and `-h`, fireaid also

- prints the help text to stdout, so that `tool --help | less` works,
- prints it as it is, where Fire starts a pager on a terminal,
- omits Fire's `INFO: Showing help with the command ...` banner,
- shows help for functions taking `**kwargs`, where Fire passes
  `help=True` to the function.

Native Fire syntax continues to work unchanged, printing to stderr,
or to a pager on a terminal.

## Usage text

A command line that names a group rather than a command, such as
plain `tool`, is a success to Fire, which shows the full help for the
group, in a pager. To fireaid it is an incomplete command, as it is to
Git: it prints Fire's short usage text to stdout, and exits with 2,
Fire's exit code for errors:

    Usage: tool <group|command>
      available groups:      shelf
      available commands:    add | help | list | remove | search

    For detailed information on this command, run:
      tool --help

A function that returns an object is a complete command. There, Fire
shows the help for the object, which fireaid only keeps from the pager.

For a program that is a class with constructor flags, Fire's usage reads
`tool - <group|command>`, the `-` standing in for the flags. Since the
flags may come anywhere, fireaid leaves it out.

## Colour

On a terminal, fireaid colours the usage text and the help it prints:
headings light blue and bold, the names of commands and flags light
green, and placeholders for argument values yellow. Output that is
redirected stays plain, as does output to a terminal that has no
colours according to `TERM`, and everything with `NO_COLOR` set.
`FORCE_COLOR` switches colour on regardless. Help in Fire's native
syntax looks as Fire makes it.

## Examples

`examples/pantry.py` is a small program to try the help, the usage
text and the colour on, and `examples/tour.sh` runs through it.

`examples/reminders.py` takes dates, times, durations, an `Enum` and
numbers by their type hints, and `examples/hints_tour.sh` shows what
each gives, and the usage errors for what does not fit.

`examples/lamp.py` is remote controlled, and allows the commands that
run remotely, so that its firmware command stays local.
`examples/remote_tour.sh` runs a server for it on this computer, and
commands through it, after showing that `pantry.py`, which does not ask
for remote control, has none.

`examples/progress.py` is a template for a tool that does longer work:
log lines in colour and a progress bar on a terminal, plain lines
everywhere else, drawn by [rich](https://pypi.org/project/rich/).

## Remote control

**Remote control is off unless the program turns it on, in its own
call of `fire.Fire`, with `remote=True` or a `fireaid.Remote`.** Without
that, nothing of it exists: there is no `server` command, `--remote` and
`TOOL_REMOTE` are a flag and a variable like any other, left to the
program, and the code for remote control is not even imported. No flag,
environment variable or setting turns it on from outside the program,
and in it only `True` or a `fireaid.Remote` does: anything else that
might pass for yes, such as `1` or `"yes"`, is an error.

A program can then be run on another computer, such as the one its
hardware is plugged into:

    fire.Fire(MyTool, remote=True)

adds a `server` command, which serves the program's commands:

    tool server
    tool server --port 4247 --password secret
    tool server --bind 127.0.0.1

and a `--remote [PASSWORD@]HOST[:PORT]` flag, which runs a command line
on such a server, anywhere on the command line before a `--`:

    tool --remote secret@boat get battery
    tool get battery --remote secret@boat

The variable `TOOL_REMOTE`, named after the program, gives a default for
the flag, and `--remote=` runs a command locally all the same. On the
server, the variable `TOOL_SERVER_PASSWORD` gives the password where
`--password` does not, keeping it out of the server's process list; the
commands the server runs do not get it.

The help lists `--remote` among the flags of the program, its groups and
its commands, except for `help`, `server` and what does not run
remotely.

Each command runs in a new process of the program on the server, as if
typed there, and its output, input and exit code are relayed. Where the
client has a terminal, the command gets one of the same size, so that
colour, progress bars, line width, prompts and Ctrl-C work as they do
locally; where the client has a pipe or a file, the command gets a pipe.
The client's `TERM`, `NO_COLOR`, `FORCE_COLOR`, `COLUMNS` and locale
settings go along, not its `PAGER`, which Fire runs in a shell.
Input goes to the server as the command reads it, so that a command
that reads slowly, or not at all, keeps it waiting at the client. A
command is done when it exits, even if something it started, such as a
daemon, still holds its output.

Where the client's terminal passes keys straight on, Ctrl-C is a key
for the command's terminal on the server. To leave a command that does
not end, type Enter, `~` and `.`, as in ssh: the client disconnects
with exit code 255, and the server ends the command.

With `remote=True`, every command runs remotely. A program that
controls something physical does better to name the commands that do,
in a `fireaid.Remote`, so that a command it gains later stays local
until it is allowed:

    fire.Fire(MyTool, remote=fire.Remote(allow=("status", "on", "off")))

A command line must then start with one of them, or ask for help for
one; what a chain of commands reaches from there is that command's.
The program's help and usage, and help for `help` and `server`, are
always there. A flag with a value goes after the command, since before
it the value cannot be told from a command.

Commands that must only run locally are named in `deny`:

    fire.Fire(MyTool, remote=fire.Remote(deny=("firmware",)))

The server refuses them wherever they appear on a command line, help
for them included, since Fire chains commands, and whether they are
allowed or not: `allow=("db",), deny=("db drop",)` allows every `db`
command but `db drop`. The server also refuses anything after a `--`:
Fire's own flags, its interactive Python shell among them, stay local,
and `--help` works without the separator.
`fireaid.Remote` also sets the default port, 4247, and with `env` the
name of the variable in place of `TOOL_REMOTE`. `TOOL_SERVER_PASSWORD`
is always named after the program.

The server logs through the `fireaid.remote` logger: `Remote(setup=f)`
names a function the server calls with its `--debug` flag before it
starts, where the program sets up its own logging; without one, the
server prints plain lines. Each command is logged by its words up to the
first flag, since a flag's value may be a password; `--debug` logs each
client and its whole command line.

The server listens on all of the computer's addresses unless `--bind`
names one. Without a password, anyone who can reach the port may run the
program's commands, which is the normal state of affairs on a network of
one's own; the server says so once when it starts. With a password,
client and server prove to each other that they know it, by HMAC
challenge-response: the password never crosses the network, a recorded
login does not work a second time, and a client sends its command only
to a server that knows the password, never to one that asks for none.
That is mild security. The connection is not encrypted or protected
otherwise, so whoever can watch it can read the commands and their
output, take over a session once it has started, and try passwords
against a recorded login.

The server runs one command at a time. A client that logs in meanwhile
waits a few seconds for it to end, then is told the server is busy;
clients slow to log in keep no other out. Server and client notice
within a minute or so when the other has vanished without a word, as a
computer gone to sleep does.

A server on Windows, which has no pseudo-terminals, gives every command
pipes: the command behaves as if its output were redirected, and Ctrl-C
at the client ends it. A client on Windows gets the full treatment from
a server on Linux or macOS; the two kinds work together either way.

## Limits

fireaid does not take anything away from the wrapped program. The one
thing it does differently from Fire on purpose is the values hinted
parameters get, as [Type hints](#type-hints) describes. Otherwise:

- `help` is only help as the first word, and not if the program has
  a `help` command of its own, or is a single function.
- `--help` and `-h` are only help as the last word. `--help` is left
  to a command with a `help` parameter, `-h` to a command with a
  parameter starting with `h`, for which it is Fire's shortcut.
- A command that fireaid cannot find without running the program, such
  as a member created in `__init__`, may have such a parameter. There,
  `-h` is left to Fire, which shows help with its banner on stderr.
- With `remote=True`, `--remote` belongs to fireaid on every command
  line, even one whose command only runs locally, and whose help does
  not list it.

Help for a command that does not exist is an error, as it is in
Fire's native syntax.

As with Fire, help is shown for what the command line evaluates to.
`tool foo x --help` calls `foo` and shows help for its result. Ask
for `tool foo --help` to get help for `foo`.

## Tests

    make test

creates a virtual environment in `.venv`, upgrades it to the latest
stable Fire, and runs the test suite. The tests hold fireaid to what
this README says, mostly by comparing it to Fire on the same command
lines. `make test PYTHON=python3.14` selects the Python for a new
environment.

    make test-min

runs the tests against the oldest Fire that `pyproject.toml` allows, in
`.venv-min`. That Fire needs a Python before 3.13: `python3.12` by
default, another with `PYTHON_MIN=python3.11`. `make clean` removes both
environments.

## Releasing

Set the new `version` in `pyproject.toml`, commit, then tag and push:

    git tag v0.2.0 && git push origin main v0.2.0

The tag starts `.github/workflows/publish.yml`, which runs the tests
as every push does, on Linux, macOS and Windows and against the oldest
Fire, checks that the tag is the package version, builds, and publishes
to PyPI as a trusted publisher. PyPI never takes the same version twice.
