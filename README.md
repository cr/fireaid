# fireaid

*A colorful little help for Python Fire.*

[Python Fire](https://pypi.org/project/fire/) turns any Python
function, class or module into a command-line tool with a single line
of code. `fireaid` is a thin wrapper around it that adds the help
conventions people know from tools like `git`: a `help` command,
`--help` printed straight to the terminal, a short usage message when
a command is incomplete, and colour that makes it all easy to read.
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
`--help` without a separator. On Python 3.13 and later, Fire itself
requires version 0.7.0.

## Usage

Change:

    import fire

to:

    import fireaid as fire

Existing code such as:

    fire.Fire(MyCLI)

continues to work, as do `import fireaid.core` and `python -m fireaid`.

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
Fire runs: to its class, or to a copy if the program is a dict.

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
headings light blue and bold, the names of commands and flags light green, and
placeholders for argument values yellow. Output that is redirected
stays plain, as does output to a terminal that has no colours
according to `TERM`, and everything with `NO_COLOR` set. `FORCE_COLOR`
switches colour on regardless. Help in Fire's native syntax looks as
Fire makes it.

`examples/pantry.py` is a small program to try all of this on, and
`examples/tour.sh` runs through it.

`examples/progress.py` is a template for a tool that does longer work:
log lines in colour and a progress bar on a terminal, plain lines
everywhere else, drawn by [rich](https://pypi.org/project/rich/).

## Remote control

A program can be run on another computer, such as the one its hardware
is plugged into:

    fire.Fire(MyTool, remote=True)

adds a `server` command, which serves the program's commands:

    tool server
    tool server --port 4247 --password secret

and a `--remote [PASSWORD@]HOST[:PORT]` flag, which runs a command line
on such a server, anywhere on the command line before a `--`:

    tool --remote secret@boat get battery
    tool get battery --remote secret@boat

The variable `TOOL_REMOTE`, named after the program, gives a default for
the flag, and `--remote=` runs a command locally all the same.

Each command runs in a new process of the program on the server, as if
typed there, and its output, input and exit code are relayed. Where the
client has a terminal, the command gets one of the same size, so that
colour, progress bars, line width, prompts and Ctrl-C work as they do
locally; where the client has a pipe or a file, the command gets a pipe.
The client's `TERM`, `NO_COLOR`, `FORCE_COLOR`, `COLUMNS`, locale and
pager settings go along.

Commands that must only run locally are named in a `fireaid.Remote`:

    fire.Fire(MyTool, remote=fire.Remote(deny=("firmware",)))

The server refuses them, help for them included. `fireaid.Remote` also
sets the default port, 4247, and the name of the variable.

Without a password, the server only accepts connections from its own
computer. With one, it accepts them from anywhere, and the client must
know the password, which itself never crosses the network. The
connection is not encrypted, so keep it to a network you trust. The
server serves one client at a time.

A server on Windows, which has no pseudo-terminals, gives every command
pipes: the command behaves as if its output were redirected, and Ctrl-C
at the client ends it. A client on Windows gets the full treatment from
a server on Linux or macOS; the two kinds work together either way.

## Limits

fireaid does not take anything away from the wrapped program:

- `help` is only help as the first word, and not if the program has
  a `help` command of its own, or is a single function.
- `--help` and `-h` are only help as the last word. `--help` is left
  to a command with a `help` parameter, `-h` to a command with a
  parameter starting with `h`, for which it is Fire's shortcut.
- A command that fireaid cannot find without running the program, such
  as a member created in `__init__`, may have such a parameter. There,
  `-h` is left to Fire, which shows help with its banner on stderr.
- With `remote=True`, `--remote` belongs to fireaid on every command
  line, and is not listed in the help for each command. `tool help
  server` describes it.

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
environment, `make clean` removes it.

## Releasing

Set the new `version` in `pyproject.toml`, commit, then tag and push:

    git tag v0.2.0 && git push origin main v0.2.0

The tag starts `.github/workflows/publish.yml`, which runs the tests,
checks that the tag is the package version, builds, and publishes to
PyPI as a trusted publisher. PyPI never takes the same version twice.
