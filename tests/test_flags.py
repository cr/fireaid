"""
A flag a routine does not take: refused before the routine runs.

Fire calls a routine with the flags it takes, and complains about the
rest only afterwards. fireaid refuses them first, with Fire's usage
error, and leaves Fire everything else it makes of flags.
"""

import pytest

import fireaid


@pytest.mark.parametrize(
    "command, error",
    [
        ("effect --wrod x", "Could not consume arg: --wrod"),
        ("effect --wrod=x", "Could not consume arg: --wrod=x"),
        ("effect x -q", "Could not consume arg: -q"),
        ("effect x --wrod --nonesuch=1", "Could not consume args: --wrod --nonesuch=1"),
        # A misspelt flag after a correct one.
        ("effect --word x --forse", "Could not consume arg: --forse"),
    ],
)
def test_flag_not_taken_is_refused_before_the_call(run, command, error):
    result = run(command)
    assert result.code == 2
    assert result.out == ""
    assert result.err.startswith(f"ERROR: {error}\nUsage: cli.py effect <flags>\n")
    # Fire runs the command first.
    assert run(command, module="fire").out.startswith("EFFECT\n")


def test_refusal_with_help_shows_help(run):
    # As Fire does for an error on a command line with --help on it.
    result = run("effect x --help --wrod")
    assert result.code == 2
    assert result.out == ""
    assert "cli.py effect - Effect doc." in result.err


@pytest.mark.parametrize(
    "component, command, out",
    [
        # Flags the routine takes, in Fire's every way.
        ("CLI", "foo x --count 2", "foo name='x' count=2\n"),
        ("CLI", "foo -n x -c 2", "foo name='x' count=2\n"),
        ("CLI", "grep pat --h", "grep pattern='pat' h=True\n"),
        ("CLI", "grep pat --noh", "grep pattern='pat' h=False\n"),
        ("CLI", "kw --a 1 --b=x", "kw [('a', 1), ('b', 'x')]\n"),
        # Not flags.
        ("CLI", "echo -5 -1.5", "-5 -1.5\n"),
        ("CLI", "foo x -- --nonesuch", "foo name='x' count=1\n"),
        # Flags for what a routine returns, after a -.
        ("CLI", "chain - bar --x 3", "bar x=3\n"),
        ("CLI", "foo x - --nonesuch", None),
        # Flags for the class, after the method's name.
        ("Typed", "who --verbose", "who password=None verbose=True\n"),
        ("Configured", "foo x --verbose", "foo name='x' count=1\n"),
    ],
)
def test_flags_fire_takes_are_left_to_it(run, component, command, out):
    result = run(command, component=component)
    assert result == run(command, module="fire", component=component)
    if out is not None:
        assert (result.code, result.out, result.err) == (0, out, "")


def test_flag_for_what_a_routine_returns_needs_a_separator(run):
    # Fire would take it for bar, chained onto chain()'s result.
    assert run("chain bar --x 3", module="fire").out == "bar x=3\n"
    result = run("chain bar --x 3")
    assert result.code == 2
    assert result.err.startswith("ERROR: Could not consume arg: --x\n")


def test_help_still_comes_first(run):
    for command in ("effect --help", "effect -h", "effect --help x"):
        result = run(command)
        assert result.code == 0
        assert "EFFECT" not in result.out


class Calls:
    def __init__(self):
        self.calls = []

    def write(self, value=0, force=False):
        self.calls.append(("write", value, force))

    def amb(self, foo=0, far=0):
        self.calls.append(("amb", foo, far))


class Callable:
    def __init__(self):
        self.calls = []

    def __call__(self, value=0):
        self.calls.append(value)


def test_routine_is_not_called(capsys):
    component = Calls()
    with pytest.raises(fireaid.core.FireExit) as exit:
        fireaid.Fire(component, command=["write", "--value", "13", "--forse"], name="t")
    assert exit.value.code == 2
    assert component.calls == []
    assert "Could not consume arg: --forse\n" in capsys.readouterr().err
    fireaid.Fire(component, command=["write", "--value", "13", "--force"], name="t")
    assert component.calls == [("write", 13, True)]


def test_callable_object_is_not_called(capsys):
    component = Callable()
    with pytest.raises(fireaid.core.FireExit):
        fireaid.Fire(component, command=["--valeu", "13"], name="t")
    assert component.calls == []
    fireaid.Fire(component, command=["--value", "13"], name="t")
    assert component.calls == [13]


def test_ambiguous_short_flag_fails_as_fire_has_it(capsys):
    component = Calls()
    with pytest.raises(fireaid.core.FireExit):
        fireaid.Fire(component, command=["amb", "-f", "1"], name="t")
    assert component.calls == []
    assert "The argument '-f' is ambiguous" in capsys.readouterr().err


def test_nested_fire_is_left_alone(capsys):
    calls = []

    def inner():
        calls.append("inner")

    def outer():
        # The command's own business: plain Fire calls inner first.
        return fireaid.core.Fire(inner, command=["--nonesuch"], name="inner")

    with pytest.raises(fireaid.core.FireExit):
        fireaid.Fire(outer, command=[], name="t")
    assert calls == ["inner"]
