"""
fireaid as a drop-in replacement of the fire module.
"""

import importlib
import inspect
import os
from importlib import metadata

import fire
import pytest

import fireaid


class Program:
    def foo(self, name="x"):
        """Foo doc."""
        return f"foo {name}"


def test_fire_is_wrapped():
    assert fireaid.Fire is not fire.Fire
    assert fireaid.Fire.__wrapped__ is fire.Fire
    assert fireaid.Fire.__doc__ == fire.Fire.__doc__
    assert inspect.signature(fireaid.Fire) == inspect.signature(fire.Fire)


def test_attributes_are_fires():
    assert fireaid.core is fire.core
    assert fireaid.decorators is fire.decorators
    assert set(dir(fire)) <= set(dir(fireaid))
    with pytest.raises(AttributeError):
        fireaid.nonesuch


def test_submodules_are_fires():
    assert importlib.import_module("fireaid.core") is fire.core
    assert importlib.import_module("fireaid.console.console_io") is (
        importlib.import_module("fire.console.console_io")
    )
    from fireaid.core import FireExit

    assert FireExit is fire.core.FireExit


def test_star_import_is_fire_only():
    assert fireaid.__all__ == fire.__all__ == ["Fire"]
    namespace = {}
    exec("from fireaid import *", namespace)
    assert set(namespace) - {"__builtins__"} == {"Fire"}


def test_version_is_our_own():
    assert fireaid.__version__ == metadata.version("fireaid")


def test_requirement_is_the_fire_with_plain_help():
    required = [r for r in metadata.requires("fireaid") if "extra ==" not in r]
    assert required == ["fire>=0.2.0"]


@pytest.mark.parametrize(
    "command",
    [
        ["foo", "--name=y"],
        ("foo", "--name=y"),
        "foo --name=y",
        "foo --name y",
    ],
)
def test_command_argument(command, capsys):
    assert fireaid.Fire(Program, command=command) == "foo y"
    assert capsys.readouterr().out == "foo y\n"


def test_command_by_position(capsys):
    assert fireaid.Fire(Program, ["foo"]) == "foo x"


@pytest.mark.parametrize(
    "command",
    [
        ["foo", "--help"],
        ("foo", "-h"),
        ["help", "foo"],
        "foo --help",
        "foo -h",
        "help foo",
    ],
)
def test_command_argument_asking_for_help(command, capsys):
    with pytest.raises(fire.core.FireExit) as exit:
        fireaid.Fire(Program, command=command, name="tool")
    assert exit.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "tool foo - Foo doc." in captured.out


def test_command_argument_is_not_modified():
    command = ["help", "foo"]
    with pytest.raises(fire.core.FireExit):
        fireaid.Fire(Program, command=command)
    assert command == ["help", "foo"]


def test_invalid_command_is_fires_error():
    with pytest.raises(ValueError) as ours:
        fireaid.Fire(Program, command=42)
    with pytest.raises(ValueError) as fires:
        fire.Fire(Program, command=42)
    assert str(ours.value) == str(fires.value)


def test_invalid_argument_is_an_error():
    with pytest.raises(TypeError):
        fireaid.Fire(Program, nonesuch=1)


def test_incomplete_command_argument(capsys):
    with pytest.raises(fire.core.FireExit) as exit:
        fireaid.Fire(Program, command=[], name="tool")
    assert exit.value.code == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.startswith("Usage: tool <command>")


@pytest.mark.parametrize("command", ["foo", "", "--help", "nonesuch --help"])
def test_fire_is_left_as_found(command, capsys):
    display, helptext = fire.core.Display, fire.core.helptext
    assert helptext is fire.helptext
    try:
        fireaid.Fire(Program, command=command)
    except fire.core.FireExit:
        pass
    assert fire.core.Display is display
    assert fire.core.helptext is helptext


def test_run_as_module(run_module):
    result = run_module("os.path join a b")
    assert result.code == 0
    assert result.out == os.path.join("a", "b") + "\n"
    assert result == run_module("os.path join a b", module="fire")


def test_run_as_module_on_a_file(run_module):
    result = run_module("cli.py CLI foo x")
    assert result.code == 0
    assert result.out == "foo name='x' count=1\n"
    assert result == run_module("cli.py CLI foo x", module="fire")


def test_run_as_module_with_help(run_module):
    native = run_module("os.path join -- --help", module="fire")
    for command in ("os.path join --help", "os.path help join"):
        result = run_module(command)
        assert result.code == 0
        assert result.err == ""
        assert result.out == native.err


def test_run_as_module_without_a_module(run_module):
    result = run_module("")
    assert result.code == 1
    assert result.out.startswith("usage: python -m fireaid")


def test_help_command_in_native_syntax(capsys):
    with pytest.raises(fire.core.FireExit) as exit:
        fireaid.Fire(Program, command="help foo -- --verbose", name="tool")
    assert exit.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "tool foo - Foo doc." in captured.out


@pytest.mark.parametrize("command", ["foo", "", "help", "help help", "nonesuch"])
def test_component_is_left_as_found(command, capsys):
    import types

    module = types.ModuleType("module")
    module.foo = Program().foo
    instance = Program()
    mapping = {"foo": instance.foo}
    for component in (Program, instance, module, mapping):
        try:
            fireaid.Fire(component, command=command)
        except fire.core.FireExit:
            pass
    assert not hasattr(Program, "help")
    assert not hasattr(module, "help")
    assert vars(instance) == {}
    assert list(mapping) == ["foo"]


def test_component_that_cannot_take_a_help_command(capsys):
    # A list has no commands of its own, and its class is not ours to touch.
    assert fireaid.Fire([1, 2], command="1") == 2
    with pytest.raises(fire.core.FireExit) as exit:
        fireaid.Fire([1, 2], command="help")
    assert exit.value.code == 0
