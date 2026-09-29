# fireaid

`fireaid` is a thin, transparent wrapper around
[Google Python Fire](https://github.com/google/python-fire).

It adds conventional command-line help syntax while otherwise delegating
to Python Fire.

## Installation

From the project directory:

    pip install .

For development:

    pip install -e .

## Usage

Change:

    import fire

to:

    import fireaid as fire

Existing code such as:

    fire.Fire(MyCLI)

continues to work.

## Help

Python Fire normally expects:

    tool foo -- --help

fireaid additionally supports:

    tool --help
    tool -h
    tool foo --help
    tool foo -h

and Git-style help:

    tool help
    tool help foo
    tool help foo bar

Native Fire syntax continues to work unchanged.
