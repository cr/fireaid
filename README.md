# fireaid

*A little help for Python Fire.*

[Python Fire](https://github.com/google/python-fire) turns any Python
function, class or module into a command-line tool with a single line
of code. `fireaid` is a thin wrapper around it that adds the help
conventions people know from tools like `git`: a `help` command,
`--help` printed plainly to the terminal, and a short usage message
when a command is incomplete. Everything else is Fire, unchanged.

All you need is:

    pip install git+https://github.com/cr/fireaid

    import fireaid as fire

    fire.Fire(MyTool)

## Installation

Directly from the repository:

    pip install git+https://github.com/cr/fireaid

As a dependency in another project's `pyproject.toml`:

    dependencies = ["fireaid @ git+https://github.com/cr/fireaid"]

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

`examples/pantry.py` is a small program to try all of this on, and
`examples/tour.sh` runs through it.

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
