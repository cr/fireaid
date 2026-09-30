"""
The program under test. Not a test module itself.

FIREAID_TEST_MODULE selects fire or fireaid, FIREAID_TEST_COMPONENT the
component passed to Fire(), NONE standing for no component at all.
FIREAID_TEST_HELP gives the component fireaid's help command up front,
to see what Fire makes of it as a command like any other.
"""

import os

fire = __import__(os.environ.get("FIREAID_TEST_MODULE", "fireaid"))


class Sub:
    def bar(self, x=1):
        """Bar doc."""
        return f"bar x={x}"

    def hosty(self, host="localhost"):
        """Hosty doc."""
        return f"hosty host={host}"


class CLI:
    """Top doc."""

    static = Sub()

    def __init__(self):
        self.dynamic = Sub()

    def foo(self, name, count=1):
        """Foo doc."""
        return f"foo name={name!r} count={count}"

    def grep(self, pattern, h=False):
        """Grep doc."""
        return f"grep pattern={pattern!r} h={h}"

    def dash_name(self, height=1):
        """Dash doc."""
        return f"dash_name height={height}"

    def echo(self, *words):
        """Echo doc."""
        return " ".join(map(str, words))

    def kw(self, **opts):
        """Kw doc."""
        return f"kw {sorted(opts.items())}"

    def effect(self, word="result"):
        """Effect doc."""
        print("EFFECT")
        return word


class WithHelp(CLI):
    def help(self, topic="general"):
        """The program's own help command."""
        return f"own help topic={topic}"


def fn(word, help=False):
    """Fn doc."""
    return f"fn word={word!r} help={help}"


def make():
    """Make doc."""
    return Sub()


DICT = {"foo": CLI().foo, "help": lambda: "own help"}

INSTANCE = CLI()


if __name__ == "__main__":
    which = os.environ.get("FIREAID_TEST_COMPONENT", "CLI")
    if which == "NONE":
        fire.Fire()
    else:
        component = globals()[which]
        if os.environ.get("FIREAID_TEST_HELP"):
            import fireaid

            component, _ = fireaid._add_help(component)
        fire.Fire(component)
