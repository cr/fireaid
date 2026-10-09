"""
The examples run, and show what they say they show.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parent.parent / "examples"


def test_tour():
    completed = subprocess.run(
        ["sh", str(EXAMPLES / "tour.sh")],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={"PATH": str(Path(sys.executable).parent) + ":/usr/bin:/bin"},
    )
    assert completed.returncode == 0
    assert completed.stderr == ""
    out = completed.stdout
    assert "Usage: pantry.py <group|command>\n" in out
    assert "(exit code 2)" in out
    assert "  available commands:    add | help | list | remove | search\n" in out
    assert "pantry.py help - Show help for the program, or for a command." in out
    assert "Usage: pantry.py shelf <command>\n" in out
    assert out.count("pantry.py - Keep track of what is in the pantry.") == 3
    assert out.count("pantry.py add - Put an item into the pantry.") == 3
    assert "pantry.py shelf label - Label the shelf." in out
    assert "pantry.py list - List the pantry" in out
    assert "Searching for 'milk' everywhere." in out
    assert "(nothing, it all went to stderr)" in out
    assert 'The string "Added 2 x milk."' in out


def progress(*args, **env):
    """Run examples/progress.py, which needs rich, without a terminal"""
    pytest.importorskip("rich")
    clean = {key: value for key, value in os.environ.items() if key not in ("FORCE_COLOR", "NO_COLOR")}
    return subprocess.run(
        [sys.executable, str(EXAMPLES / "progress.py"), *args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env={**clean, **env},
    )


def test_progress_is_plain_lines_without_a_terminal():
    completed = progress("copy", "--size=40000")
    assert completed.returncode == 0
    assert completed.stdout == "20 chunks\n", "the result, and nothing else, on stdout"
    err = completed.stderr
    assert "\x1b[" not in err, "no terminal control codes in a pipe"
    assert " INFO Copying 40000 bytes in chunks of 2048\n" in err
    assert " INFO 20480 / 40000 bytes (50%)\n" in err, "progress is logged instead of drawn"
    assert " WARNING Half way" in err and " INFO Done\n" in err
    assert " DEBUG " not in err


def test_progress_is_drawn_on_a_terminal():
    completed = progress("copy", "--size=40000", FORCE_COLOR="1")  # rich then treats stderr as a terminal
    assert completed.returncode == 0
    assert completed.stdout == "20 chunks\n"
    err = completed.stderr
    assert "\x1b[" in err, "styled output"
    assert "Copying" in err and "40000/40000" in err, "the bar was drawn"
    assert "bytes (50%)" not in err, "and replaces the progress log lines"
    assert "Half way" in err, "log lines appear while the bar is up"


def test_progress_debug_and_levels():
    err = progress("copy", "--size=4096", "--debug").stderr
    assert " DEBUG progress Chunk at 2048\n" in err, "debug lines, with the logger's name"
    err = progress("levels").stderr
    for level in "INFO", "WARNING", "ERROR", "CRITICAL":
        assert f" {level} " in err
    assert " DEBUG " not in err and "ValueError: invalid literal" in err
    assert "progress.py copy - Copy, showing progress." in progress("help", "copy").stdout
